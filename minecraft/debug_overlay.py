"""
minecraft/debug_overlay.py — reading the game's own numbers off the F3 screen.

WHY THIS EXISTS
    Phase 2 could see the screen and understand none of it. Everything above —
    verification, task running, any planning at all — needs to answer "did that
    do anything?", and you cannot answer that from a JPEG.

    Minecraft already computes and displays exactly the numbers needed. F3
    puts the player's position, facing, biome, light level and the block under
    the crosshair on screen, in a fixed layout, in vanilla, with no mod. This
    module turns that display back into structured state.

THE SPLIT THAT MATTERS
    Getting pixels into text is unreliable. Turning that text into state is
    not. So they are separate:

        capture ──▶ TextReader (unreliable, optional, swappable)
                        │
                        ▼
                    parse_overlay()  (pure, deterministic, fully tested)
                        │
                        ▼
                    WorldState

    `parse_overlay` is a pure function over a string. Every test in
    tests/test_minecraft_debug_overlay.py runs against captured F3 text with
    no screen, no OCR engine and no Minecraft. That is the part that has to be
    right, and it is the part that can actually be proven right here.

    The reader is the part I cannot verify without your machine. It is behind
    an interface, it is optional, and when it is missing this source says so
    and returns an empty state rather than guessing.

WHAT F3 CANNOT TELL US
    Health, hunger, inventory, held item, nearby entities, weather and the
    world time are NOT on the debug screen. They are not parsed, not inferred
    from pixels, and not guessed — they stay None with provenance "unknown".

    Reading a health bar by counting red pixels is possible and is deliberately
    not done: it is right most of the time, and the times it is wrong are the
    times the player is about to die. Those fields wait for the mod bridge,
    which can read them from the game rather than from a picture of the game.

NO OCR LIBRARY IS IMPORTED HERE
    This module declares the `TextReader` interface and is handed one. It does
    not import pytesseract, and must not.

    pytesseract runs the Tesseract binary in a subprocess, and this package is
    forbidden from starting processes -- `tests/test_minecraft_boundary.py`
    parses every module here and fails the build on it. That rule is what
    stops "JARVIS can play Minecraft" from quietly becoming "JARVIS can run
    programs", so the reader implementation lives in `core/ocr.py` and is
    injected by `actions/minecraft.py`. The Minecraft package keeps its bright
    line; the subprocess stays in core, under the rest of the app's rules.

    Whatever reader arrives, if none does the source is honestly unavailable
    and says what to install. Nothing else in the app degrades.
"""

from __future__ import annotations

import re

from minecraft.state import (
    BlockRef, INFERRED, UNKNOWN, WorldState, empty_state,
)

# ── The patterns ─────────────────────────────────────────────────────────────
#
# Tolerant about whitespace and separators, strict about shape. A line that
# does not match leaves its field None: a half-read coordinate is worse than a
# missing one, because the planner would act on it.

_XYZ = re.compile(
    r"XYZ[:\s]+(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)",
    re.IGNORECASE)

_BLOCK = re.compile(r"^\s*Block[:\s]+(-?\d+)[,\s]+(-?\d+)[,\s]+(-?\d+)",
                    re.IGNORECASE | re.MULTILINE)

# "Facing: north (Towards negative Z) (-179.9 / 12.3)"
_FACING = re.compile(
    r"Facing[:\s]+([a-z]+)"                      # cardinal
    r"(?:[^()]*\([^()]*\))?"                     # the "(Towards ...)" aside
    r"\s*\(\s*(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)\s*\)",
    re.IGNORECASE)

# Cardinal alone, for builds whose Facing line omits the angles.
_FACING_ONLY = re.compile(r"Facing[:\s]+(north|south|east|west)\b",
                          re.IGNORECASE)

_BIOME = re.compile(r"Biome[:\s]+(?:minecraft:)?([a-z_]+)", re.IGNORECASE)

_DIMENSION = re.compile(r"Dimension[:\s]+(?:minecraft:)?([a-z_]+)",
                        re.IGNORECASE)

_LIGHT = re.compile(r"Client\s+Light[:\s]+(\d+)", re.IGNORECASE)

# "Targeted Block: 123, 63, -789" — the block id is on a following line.
_TARGET_POS = re.compile(
    r"Targeted\s+Block[:\s]+(-?\d+)[,\s]+(-?\d+)[,\s]+(-?\d+)", re.IGNORECASE)

_NAMESPACED = re.compile(r"\bminecraft:([a-z_]+)\b", re.IGNORECASE)

# "axis: y", "facing: north", "half: bottom" — block state lines under the
# targeted block. Only the axis/face is taken; the rest is block-specific.
_TARGET_FACE = re.compile(r"^\s*(?:axis|facing)[:\s]+([a-z_]+)\s*$",
                          re.IGNORECASE | re.MULTILINE)

_CARDINALS = frozenset({"north", "south", "east", "west"})

# A line that is present on every F3 screen and almost nowhere else. Used to
# tell "the overlay is closed" from "OCR produced noise", which are different
# problems with different fixes.
_OVERLAY_MARKERS = ("xyz", "facing", "biome", "chunk", "client light")


def looks_like_overlay(text: str) -> bool:
    """Is this plausibly F3 output at all?

    Two markers rather than one: a single stray word can appear in a chat
    message or a sign, and calling that an overlay would produce a state built
    from someone's text."""
    if not text:
        return False
    low = text.lower()
    return sum(marker in low for marker in _OVERLAY_MARKERS) >= 2


def _num(raw: str, as_int: bool = False):
    try:
        return int(raw) if as_int else float(raw)
    except (TypeError, ValueError):
        return None


def parse_overlay(text: str) -> dict:
    """F3 text to a dict of the fields that were actually found.

    Pure. No I/O, no screen, no guessing — a field appears in the result only
    if its line matched. The caller decides what to do with the gaps; this
    function never fills one in.
    """
    found: dict = {}
    if not text:
        return found

    match = _XYZ.search(text)
    if match:
        x, y, z = (_num(g) for g in match.groups())
        if None not in (x, y, z):
            found["position"] = (x, y, z)

    match = _BLOCK.search(text)
    if match:
        bx, by, bz = (_num(g, as_int=True) for g in match.groups())
        if None not in (bx, by, bz):
            found["block_position"] = (bx, by, bz)

    match = _FACING.search(text)
    if match:
        cardinal = match.group(1).lower()
        yaw, pitch = _num(match.group(2)), _num(match.group(3))
        if cardinal in _CARDINALS:
            found["facing"] = cardinal
        if yaw is not None and pitch is not None:
            found["rotation"] = (yaw, pitch)
    else:
        match = _FACING_ONLY.search(text)
        if match:
            found["facing"] = match.group(1).lower()

    match = _BIOME.search(text)
    if match:
        found["biome"] = match.group(1).lower()

    match = _DIMENSION.search(text)
    if match:
        found["dimension"] = match.group(1).lower()

    match = _LIGHT.search(text)
    if match:
        light = _num(match.group(1), as_int=True)
        # 0-15 is the whole range. Anything else is a misread, not a reading.
        if light is not None and 0 <= light <= 15:
            found["light_level"] = light

    target = _parse_target(text)
    if target is not None:
        found["target_block"] = target

    return found


def _parse_target(text: str) -> BlockRef | None:
    """The block under the crosshair.

    Returns None when there is no "Targeted Block" section, which is the normal
    case for looking at the sky — that is an answer, not a failure, and the
    caller distinguishes them by whether the overlay was readable at all."""
    match = _TARGET_POS.search(text)
    if not match:
        return None

    x, y, z = (_num(g, as_int=True) for g in match.groups())
    if None in (x, y, z):
        return None

    # The block id is the first namespaced name AFTER the coordinates — before
    # them sits the biome, which is also namespaced and would otherwise be
    # picked up as the block the player is looking at.
    tail = text[match.end():]
    name_match = _NAMESPACED.search(tail)
    name = name_match.group(1).lower() if name_match else None

    face_match = _TARGET_FACE.search(tail)
    face = face_match.group(1).lower() if face_match else None

    return BlockRef(x=x, y=y, z=z, name=name, face=face)


# Fields this source can supply, and the ones it provably cannot. Exposed so a
# status report and the tests agree on the limitation rather than restating it.
SUPPLIES = ("position", "rotation", "facing", "biome", "dimension",
            "light_level", "target_block")

CANNOT_SUPPLY = ("health", "hunger", "inventory", "selected_slot",
                 "held_item", "target_entity", "weather", "time_of_day",
                 "nearby_entities")

NEEDS_MOD_BRIDGE = (
    "Health, hunger, inventory, held item, nearby entities, weather and world "
    "time are not on the debug screen. They need the mod bridge; I will not "
    "guess them from pixels."
)


# ── Getting text out of a frame ──────────────────────────────────────────────

class TextReader:
    """Frame bytes to text. The unreliable half, kept swappable — and kept
    OUT of this package, for the reason in the module docstring.

    An implementation needs three members: `name`, `available`, and
    `read_text(frame) -> str`. `core/ocr.py` supplies one."""

    name = "none"
    available = False

    def read_text(self, frame: bytes) -> str: ...      # pragma: no cover

    def describe(self) -> str: ...                     # pragma: no cover


class NoReader:
    """The default when nothing was injected.

    Not an error at construction: a `DebugOverlayStateSource` with no reader is
    a perfectly good object that reports itself unavailable, which is what the
    status line and the tests want."""

    name = "none"
    available = False

    def __init__(self, reason: str = ""):
        self._reason = reason or (
            "No text reader is attached, so I cannot read the F3 overlay. "
            "See core/ocr.py for how to enable it."
        )

    def read_text(self, frame: bytes) -> str:
        return ""

    def describe(self) -> str:
        return self._reason


# ── The state source ─────────────────────────────────────────────────────────

class DebugOverlayStateSource:
    """Reads state from the F3 debug overlay.

    Plugs in behind `StateSource` exactly as `VisionStateSource` does, so
    nothing above it changes when a mod bridge replaces it later."""

    name = "f3-overlay"
    confidence = INFERRED

    def __init__(self, observer=None, reader=None):
        self._observer = observer
        # Injected, never constructed here — this module imports no OCR
        # library and has no way to build one.
        self._reader = reader if reader is not None else NoReader()

    @property
    def reader(self):
        return self._reader

    def available(self) -> bool:
        return bool(self._observer) and bool(getattr(self.reader, "available",
                                                     False))

    def unavailable_reason(self) -> str:
        if not self._observer:
            return "No observer is attached, so I cannot capture the screen."
        if not getattr(self.reader, "available", False):
            return self.reader.describe()
        return ""

    def read(self) -> WorldState:
        """One reading. Never raises — every failure becomes an empty state
        with a note saying which step failed, because a planner that gets an
        exception here has no state at all and one that gets an empty state
        can still decide to ask the user."""
        if not self.available():
            return empty_state(self.unavailable_reason())

        try:
            # Native resolution, not the compressed frame. The downscale that
            # makes a frame cheap for a vision model reduces F3 text to a
            # handful of pixels and JPEG-smears what survives -- see the note
            # in minecraft/observation.py.
            observation = self._observer.capture(compress=False)
        except Exception as e:
            return empty_state(f"Screen capture failed ({type(e).__name__}).")

        if not observation.ok or not observation.frame:
            return empty_state(observation.error or "Could not capture the "
                                                    "Minecraft window.")

        try:
            text = self.reader.read_text(observation.frame)
        except Exception as e:
            return empty_state(f"Could not read text from the frame "
                               f"({type(e).__name__}).")

        if not looks_like_overlay(text):
            return empty_state(
                "The F3 debug overlay does not appear to be open. Ask me to "
                "turn it on, or press F3 in the game — without it I cannot "
                "read your position or what you are looking at."
            )

        found = parse_overlay(text)
        if not found:
            return empty_state(
                "The debug overlay is open but I could not read any values "
                "from it. A larger game window, or a bigger GUI scale, makes "
                "the text easier to read."
            )

        return self._build(found, observation.timestamp)

    def _build(self, found: dict, captured_at: float) -> WorldState:
        """Found fields to a WorldState.

        Everything that came off the overlay is `inferred` — it was read from
        a picture of text. Nothing else is filled in, so every field this
        source cannot supply keeps provenance `unknown` automatically.

        LOOKING AT NOTHING IS A READING, NOT A GAP
            `target_block = None` has to mean exactly one thing — "I do not
            know" — or verification cannot work: breaking the last block in
            front of you would produce an empty target slot, and an agent that
            read that as "unknown" could never confirm the one action it most
            needs to confirm.

            The overlay distinguishes these perfectly well. If it is readable
            and carries no "Targeted Block" section, the crosshair ray hit
            nothing within reach — which in Minecraft's own terms is air. So
            that becomes an explicit `BlockRef(name="air")` with `inferred`
            provenance, and None is reserved for the case where the overlay
            could not be read at all.
        """
        target = found.get("target_block")
        if target is None:
            target = BlockRef(name="air")

        provenance = {name: INFERRED for name in found
                      if name != "block_position"}
        provenance["target_block"] = INFERRED

        notes = f"read {len(found)} field(s) from the F3 overlay"
        if "block_position" in found and "position" not in found:
            notes += "; only the integer block position was legible"
        if "target_block" not in found:
            notes += "; the crosshair is not on any block"

        return WorldState(
            position=found.get("position") or found.get("block_position"),
            rotation=found.get("rotation"),
            facing=found.get("facing"),
            biome=found.get("biome"),
            dimension=found.get("dimension"),
            light_level=found.get("light_level"),
            target_block=target,
            source=self.name,
            confidence=INFERRED,
            captured_at=captured_at,
            provenance=provenance,
            notes=notes,
        )


__all__ = [
    "parse_overlay", "looks_like_overlay", "DebugOverlayStateSource",
    "TextReader", "NoReader",
    "SUPPLIES", "CANNOT_SUPPLY", "NEEDS_MOD_BRIDGE",
]
