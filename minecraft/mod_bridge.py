"""
minecraft/mod_bridge.py — reading the game's own numbers, from the game.

WHY THIS REPLACES SQUINTING AT PIXELS
    Everything the assistant wants to know, the client already computes every
    tick and then draws. The F3 route recovered a fraction of it by reading
    those pixels back with OCR: lossy, dependent on a native install, blind to
    the inventory entirely, and wrong often enough to be dangerous — a misread
    health bar is a death.

    The companion mod hands over the numbers instead. Position and rotation
    become `exact` rather than `inferred`, and health, hunger, inventory,
    held item and nearby entities become available at all for the first time.

HOW IT ARRIVES: A FILE, NOT A SOCKET
    The mod writes one JSON document, atomically, five times a second. This
    reads it.

    No port to pick, no firewall prompt, nothing listening on the machine, and
    nothing for anything else on the network to reach. It also keeps this
    package's import boundary intact: `minecraft/` may not import a network
    library, and does not need to.

    Both sides compute the same path from the same environment variable rather
    than hunting for a .minecraft directory, whose location varies by launcher
    — and where a wrong guess would be indistinguishable from the mod not
    running.

STALE DATA IS NOT DATA
    A file on disk outlives the process that wrote it. If Minecraft closed, or
    the mod is not loaded, or the game is paused in a menu, this file sits
    there looking perfectly valid and describing a world that no longer
    exists.

    So every payload carries the moment it was written, and anything older
    than `MAX_AGE_SECONDS` is discarded rather than believed. That is the same
    rule the rest of this subsystem follows: a confident wrong number is worse
    than an honest unknown, and nowhere more than here, where the numbers are
    otherwise trustworthy enough to act on without checking.

THE MOD CANNOT BE TOLD TO DO ANYTHING
    It observes and writes. It has no command channel and no listener, so this
    bridge is a one-way street: state comes out, nothing goes in. Input still
    travels the long way round, as synthetic keystrokes through the operating
    system, past the focus guard and the session authorisation.

    That matters for more than tidiness. A bridge that could also act would
    put a control channel inside the game process, where Alt-Tab, F12 and the
    focus guard could not reach it — every stop this subsystem relies on works
    precisely because input arrives from outside.
"""

from __future__ import annotations

import json
import os
import time

from minecraft.state import (
    BlockRef, EXACT, EntityRef, ItemStack, WorldState, empty_state,
)

SCHEMA = "markliv.minecraft.state/1"

MAX_AGE_SECONDS = 3.0
"""How old a reading may be before it is treated as no reading at all.

The mod publishes five times a second, so three seconds is fifteen missed
writes — comfortably past "the game stuttered" and well short of leaving a
closed game's last frame lying around as though it were current."""

ENV_OVERRIDE = "MARKLIV_STATE_FILE"
"""Point this at the state file to override the default location."""

_MAX_BYTES = 512 * 1024
"""Refuse to read anything larger. The real payload is a few kilobytes; a huge
file means something other than the mod wrote here, and parsing it is not this
module's job."""


def state_file_path() -> str:
    """Where the mod publishes. Must match MarkLivBridge.stateFile() in Java.

    Computed from the environment rather than discovered, so the two sides
    cannot disagree about it — and so "the file is not there" always means the
    mod is not running, never that this looked in the wrong place."""
    override = (os.environ.get(ENV_OVERRIDE) or "").strip().strip('"')
    if override:
        return override

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return os.path.join(base, "MarkLIV", "minecraft_state.json")

    return os.path.join(os.path.expanduser("~"), ".markliv",
                        "minecraft_state.json")


class ModBridgeStateSource:
    """Reads the companion mod's published state.

    Plugs in behind `StateSource` exactly as the vision and overlay sources
    do, so nothing above it changes when this becomes available — that seam
    existed from the first phase precisely so this could arrive without a
    rewrite."""

    name = "mod-bridge"
    confidence = EXACT

    def __init__(self, path: str | None = None, reader=None,
                 clock=None, max_age_s: float = MAX_AGE_SECONDS):
        self._path = path or state_file_path()
        # Injectable so tests never touch a real file.
        self._reader = reader if reader is not None else self._read_file
        self._clock = clock if clock is not None else time.time
        self._max_age = float(max_age_s)

    @property
    def path(self) -> str:
        return self._path

    def _read_file(self) -> str:
        size = os.path.getsize(self._path)
        if size > _MAX_BYTES:
            raise ValueError(f"state file is {size} bytes, which is far "
                             f"larger than the mod ever writes")
        with open(self._path, "rb") as handle:
            return handle.read().decode("utf-8")

    # ── availability ─────────────────────────────────────────────────────────

    def available(self) -> bool:
        """Present AND fresh. Both, because a stale file is not a source."""
        payload = self._payload()
        return payload is not None and self._age_of(payload) <= self._max_age

    def unavailable_reason(self) -> str:
        if not os.path.exists(self._path):
            return (
                "The MARK LIV bridge mod is not running. Install "
                "markliv-bridge.jar into your Minecraft mods folder and start "
                "the game with the Fabric loader — see fabric-mod/README.md. "
                f"I am looking for: {self._path}"
            )
        payload = self._payload()
        if payload is None:
            return (f"The bridge file at {self._path} could not be read as "
                    f"the mod's own format.")
        age = self._age_of(payload)
        if age > self._max_age:
            return (f"The bridge file is {age:.0f} seconds old, so Minecraft "
                    f"is probably closed or paused. I will not report stale "
                    f"positions as current.")
        return ""

    # ── reading ──────────────────────────────────────────────────────────────

    def read(self) -> WorldState:
        """One reading. Never raises: every failure becomes an empty state
        with a note, because a planner that gets an exception here has nothing
        at all, while one that gets an empty state can still decide to ask."""
        payload = self._payload()
        if payload is None:
            return empty_state(self.unavailable_reason())

        age = self._age_of(payload)
        if age > self._max_age:
            return empty_state(
                f"the bridge last wrote {age:.0f}s ago; Minecraft is probably "
                f"closed or paused")

        if not payload.get("in_game"):
            return empty_state(
                "Minecraft is running but you are not in a world — the mod "
                "reports no player, so there is nothing to read yet")

        return self._build(payload, age)

    def _payload(self):
        try:
            raw = self._reader()
        except Exception:
            return None
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schema") != SCHEMA:
            # A different schema is a different contract. Guessing at it would
            # be exactly the fabrication this subsystem refuses elsewhere.
            return None
        return payload

    def _age_of(self, payload: dict) -> float:
        try:
            written = float(payload.get("written_at_ms", 0)) / 1000.0
        except (TypeError, ValueError):
            return float("inf")
        return max(0.0, self._clock() - written)

    # ── mapping ──────────────────────────────────────────────────────────────

    def _build(self, payload: dict, age: float) -> WorldState:
        """The mod's JSON to a WorldState.

        Every field is read defensively. The mod is trusted to be honest, not
        to be bug-free: a malformed entry becomes None, which the provenance
        rule then marks unknown, rather than a zero that looks like a
        reading."""
        return WorldState(
            position=_triple(payload.get("position")),
            rotation=_pair(payload.get("rotation")),
            facing=_facing(_pair(payload.get("rotation"))),
            health=_number(payload.get("health")),
            hunger=_number(payload.get("hunger")),
            inventory=_inventory(payload.get("inventory")),
            selected_slot=_integer(payload.get("selected_slot")),
            held_item=_item(payload.get("held_item")),
            target_block=_block(payload.get("target_block")),
            target_entity=_entity(payload.get("target_entity")),
            dimension=_short_name(payload.get("dimension")),
            biome=_short_name(payload.get("biome")),
            weather=_text(payload.get("weather")),
            time_of_day=_integer(payload.get("time_of_day")),
            light_level=_integer(payload.get("light_level")),
            nearby_entities=_entities(payload.get("nearby_entities")),
            source=self.name,
            confidence=EXACT,
            captured_at=self._clock() - age,
            notes=f"read from the bridge mod, {age * 1000:.0f}ms old",
        )


# ── conversions ──────────────────────────────────────────────────────────────
#
# Each returns None rather than raising or substituting a default. None is what
# the provenance rule turns into "unknown"; a default would be a number nobody
# measured.

def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _integer(value):
    number = _number(value)
    return None if number is None else int(number)


def _text(value):
    return value if isinstance(value, str) and value else None


def _short_name(value):
    """'minecraft:plains' to 'plains'.

    The namespace is noise for everything above — a planner comparing against
    'oak_log' should not have to know about it — and a modded block keeps its
    own namespace because dropping that would make two different blocks look
    identical."""
    text = _text(value)
    if text is None:
        return None
    if text.startswith("minecraft:"):
        return text[len("minecraft:"):]
    return text


def _triple(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    parts = tuple(_number(v) for v in value)
    return None if any(p is None for p in parts) else parts


def _pair(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    parts = tuple(_number(v) for v in value)
    return None if any(p is None for p in parts) else parts


def _facing(rotation):
    """Yaw to a cardinal direction, matching what the F3 screen shows.

    Minecraft's yaw is 0 at south and increases clockwise, which is why this
    table does not start at north."""
    if rotation is None:
        return None
    yaw = rotation[0] % 360.0
    if yaw < 0:
        yaw += 360.0
    if yaw < 45 or yaw >= 315:
        return "south"
    if yaw < 135:
        return "west"
    if yaw < 225:
        return "north"
    return "east"


def _item(value):
    if not isinstance(value, dict):
        return None
    name = _short_name(value.get("name"))
    if name is None:
        return None
    return ItemStack(slot=_integer(value.get("slot")), name=name,
                     count=_integer(value.get("count")))


def _inventory(value):
    if not isinstance(value, (list, tuple)):
        return None
    items = tuple(item for item in (_item(v) for v in value)
                  if item is not None)
    # An empty tuple is a real reading -- an empty inventory -- and must not
    # collapse to None, which would mean "I could not see it".
    return items


def _block(value):
    if not isinstance(value, dict):
        return None
    name = _short_name(value.get("name"))
    if name is None:
        return None
    return BlockRef(x=_integer(value.get("x")), y=_integer(value.get("y")),
                    z=_integer(value.get("z")), name=name,
                    face=_text(value.get("face")))


def _entity(value):
    if not isinstance(value, dict):
        return None
    name = _short_name(value.get("name"))
    if name is None:
        return None
    return EntityRef(name=name, distance=_number(value.get("distance")))


def _entities(value):
    if not isinstance(value, (list, tuple)):
        return None
    return tuple(e for e in (_entity(v) for v in value) if e is not None)


__all__ = ["ModBridgeStateSource", "state_file_path", "SCHEMA",
           "MAX_AGE_SECONDS", "ENV_OVERRIDE"]
