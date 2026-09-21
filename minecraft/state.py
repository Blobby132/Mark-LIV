"""
minecraft/state.py — what the agent believes, and how sure it is.

THE POINT OF BUILDING THIS NOW
    Phase 2 cannot fill in most of these fields. That is exactly why the shape
    exists now: every later phase — F3 overlay parsing, a mod bridge, a vision
    model — plugs in behind `StateSource`, and the planner above never learns
    where a number came from. If the planner were written against whatever
    Phase 2 could scrape, it would have to be rewritten when the source
    improved.

THE RULE THIS FILE ENFORCES
    Do not hallucinate state from insufficient information.

    Every field starts as None and stays None until something can actually read
    it. `confidence` says how the values that ARE present were obtained:

        "unknown"  — nothing was read; almost everything is None
        "inferred" — read from pixels or the F3 overlay; usually right
        "exact"    — read from the game's own memory via a mod bridge

    A planner that acts on `health=None` has to handle it. A planner that acts
    on a guessed `health=20` walks into lava. The None is the useful answer.

PROVENANCE IS PER FIELD, NOT PER STATE
    One state can mix sources. The F3 overlay gives an exact position and says
    nothing about health; a later mod bridge gives both. A single state-level
    `confidence` would have to describe that with one word, and the only honest
    word would be the worst one — which throws away the fact that the position
    IS trustworthy.

    So `provenance` maps each field to its own level, and `confidence` remains
    as the summary (the weakest level among the fields that actually have
    values). `confidence_of("position")` is what a planner should ask.

THE INVARIANT `__post_init__` ENFORCES
    A field that is None has provenance "unknown". Always, with no way to
    override it.

    That is the "do not fabricate" rule made mechanical rather than
    remembered: a source cannot hand back `health=None, provenance={"health":
    "exact"}` and have anything downstream believe the health reading is
    trustworthy. Claiming knowledge of a value you do not have is rejected at
    construction, not caught in review.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

UNKNOWN = "unknown"
INFERRED = "inferred"
EXACT = "exact"

CONFIDENCE_LEVELS = (UNKNOWN, INFERRED, EXACT)


@dataclass(frozen=True)
class BlockRef:
    """A block the player is looking at."""
    x: int | None = None
    y: int | None = None
    z: int | None = None
    name: str | None = None
    face: str | None = None

    def as_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "z": self.z,
                "name": self.name, "face": self.face}


@dataclass(frozen=True)
class EntityRef:
    """A mob, animal or player nearby."""
    name: str | None = None
    distance: float | None = None
    hostile: bool | None = None

    def as_dict(self) -> dict:
        return {"name": self.name, "distance": self.distance,
                "hostile": self.hostile}


@dataclass(frozen=True)
class ItemStack:
    slot: int | None = None
    name: str | None = None
    count: int | None = None

    def as_dict(self) -> dict:
        return {"slot": self.slot, "name": self.name, "count": self.count}


@dataclass(frozen=True)
class WorldState:
    """Everything the agent believes about the game right now.

    Frozen, because a state object is a snapshot of a moment. Mutating one
    would mean two parts of a plan disagreeing about when they were looking."""

    # Where and which way
    position: tuple | None = None          # (x, y, z)
    rotation: tuple | None = None          # (yaw, pitch) in degrees
    facing: str | None = None              # cardinal: north/south/east/west

    # Condition
    health: float | None = None            # 0-20
    hunger: float | None = None            # 0-20

    # Carrying
    inventory: tuple | None = None         # tuple[ItemStack, ...]
    selected_slot: int | None = None       # 0-8
    held_item: ItemStack | None = None

    # Looking at
    target_block: BlockRef | None = None
    target_entity: EntityRef | None = None

    # Around
    dimension: str | None = None           # overworld / nether / the_end
    biome: str | None = None
    weather: str | None = None             # clear / rain / thunder
    time_of_day: int | None = None         # 0-24000 ticks
    light_level: int | None = None         # 0-15
    nearby_entities: tuple | None = None   # tuple[EntityRef, ...]

    # Provenance — never None, because "where did this come from" always has
    # an answer even when every value is missing.
    source: str = "none"
    confidence: str = UNKNOWN
    captured_at: float = field(default_factory=time.time)
    notes: str = ""

    # field name -> one of CONFIDENCE_LEVELS. Normalised and frozen in
    # __post_init__; read it through confidence_of().
    provenance: dict = field(default_factory=dict)

    # ── introspection ────────────────────────────────────────────────────────

    _FIELDS = ("position", "rotation", "facing", "health", "hunger",
               "inventory", "selected_slot", "held_item", "target_block",
               "target_entity", "dimension", "biome", "weather",
               "time_of_day", "light_level", "nearby_entities")

    def __post_init__(self):
        """Normalise provenance, then freeze it.

        Three rules, in order:
          * a field that is None is "unknown", whatever was claimed for it;
          * a field that has a value but no entry inherits the state-level
            `confidence`, so a source that sets one level for everything does
            not have to spell out sixteen entries;
          * an unrecognised level is discarded rather than trusted.
        """
        clean = {}
        for name in self._FIELDS:
            has_value = getattr(self, name) is not None
            claimed = self.provenance.get(name) if self.provenance else None
            if not has_value:
                clean[name] = UNKNOWN
            elif claimed in CONFIDENCE_LEVELS:
                clean[name] = claimed
            else:
                clean[name] = (self.confidence if self.confidence
                               in CONFIDENCE_LEVELS else UNKNOWN)
        object.__setattr__(self, "provenance", MappingProxyType(clean))

    def confidence_of(self, name: str) -> str:
        """How trustworthy one field is. The planner's real question — a state
        can hold an exact position next to an unknown health."""
        return self.provenance.get(name, UNKNOWN)

    def fields_at_least(self, level: str) -> tuple:
        """Fields known to at least `level`. Lets a planner say "only act on
        what is exact" without knowing which source supplied what."""
        if level not in CONFIDENCE_LEVELS:
            return ()
        floor = CONFIDENCE_LEVELS.index(level)
        return tuple(name for name in self._FIELDS
                     if CONFIDENCE_LEVELS.index(self.confidence_of(name)) >= floor)

    def known_fields(self) -> tuple:
        """Which fields actually have a value. The planner's first question."""
        return tuple(name for name in self._FIELDS
                     if getattr(self, name) is not None)

    def unknown_fields(self) -> tuple:
        return tuple(name for name in self._FIELDS
                     if getattr(self, name) is None)

    @property
    def is_empty(self) -> bool:
        """True when nothing at all could be read. Phase 2's normal answer."""
        return not self.known_fields()

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.captured_at)

    def as_dict(self) -> dict:
        def _unpack(value):
            if value is None:
                return None
            if hasattr(value, "as_dict"):
                return value.as_dict()
            if isinstance(value, tuple):
                return [_unpack(item) for item in value]
            return value

        out = {name: _unpack(getattr(self, name)) for name in self._FIELDS}
        out.update({
            "source": self.source,
            "confidence": self.confidence,
            "captured_at": self.captured_at,
            "known_fields": list(self.known_fields()),
            "unknown_fields": list(self.unknown_fields()),
            "provenance": {k: v for k, v in self.provenance.items()
                           if v != UNKNOWN},
            "notes": self.notes,
        })
        return out

    def describe(self) -> str:
        """A sentence that is honest about ignorance.

        Worth getting right: this is what the model reads back, and "I do not
        know where you are" produces much better planning than a confident
        wrong number."""
        known = self.known_fields()
        if not known:
            why = self.notes or "no state source is connected"
            return (f"I cannot read any game state yet ({why}). I can see the "
                    f"screen, but I do not know your position, health or "
                    f"inventory.")
        parts = [f"{name}={getattr(self, name)} [{self.confidence_of(name)}]"
                 for name in known]
        missing = self.unknown_fields()
        line = f"From {self.source}: " + ", ".join(parts)
        if missing:
            line += f". Still unknown: {', '.join(missing)}."
        return line


# ── The seam ─────────────────────────────────────────────────────────────────

class StateSource(Protocol):
    """Where a WorldState comes from.

    Three implementations are planned and only the first exists:

      VisionStateSource        — this phase. Reads nothing; returns an honest
                                 empty state.
      DebugOverlayStateSource  — parses the F3 overlay. Position, facing,
                                 biome, light. Vanilla, no mod, `inferred`.
      ModBridgeStateSource     — a client-side Fabric mod over localhost.
                                 Everything, `exact`.

    The planner depends on this Protocol and never on which one it got."""

    name: str
    confidence: str

    def read(self) -> WorldState: ...
    def available(self) -> bool: ...


class VisionStateSource:
    """The Phase 2 source: a frame exists, structured state does not.

    It would be easy to make this guess — health from counting red pixels, the
    selected slot from where the hotbar highlight is. It does not, because a
    guess that is right most of the time is worse than a None: the planner
    would trust it, and the few percent where it is wrong are the cases where
    the player dies."""

    name = "vision"
    confidence = UNKNOWN

    def __init__(self, observer=None):
        self._observer = observer

    def available(self) -> bool:
        return True

    def read(self) -> WorldState:
        return WorldState(
            source=self.name,
            confidence=UNKNOWN,
            notes="only the screen is available in this version; no numbers "
                  "are read from it",
        )


def empty_state(note: str = "") -> WorldState:
    """A state that knows nothing, for error paths that still owe a caller a
    WorldState rather than a None."""
    return WorldState(source="none", confidence=UNKNOWN,
                      notes=note or "no state source")


__all__ = [
    "WorldState", "StateSource", "VisionStateSource", "empty_state",
    "BlockRef", "EntityRef", "ItemStack",
    "UNKNOWN", "INFERRED", "EXACT", "CONFIDENCE_LEVELS",
]
