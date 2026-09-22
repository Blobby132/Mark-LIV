"""
minecraft/aiming.py — the one place that turns "look at that" into mouse pixels.

WHY THIS IS ITS OWN MODULE
    Pointing the camera is needed by navigation (face the next waypoint), by
    mining (put the crosshair on the block), by looking around, and by
    anything that comes later. Every one of those needs the same three facts —
    how far this machine's mouse turns per pixel, which way each axis runs,
    and what the current rotation is — and every copy of that arithmetic is a
    chance for two of them to disagree.

    So there is one class. Skills ask it for a delta; it is the only thing
    that knows how a delta becomes degrees.

THE THREE THINGS IT KNOWS, IN ORDER OF AUTHORITY
    1. The game's own sensitivity slider, reported by the bridge mod.
       Minecraft's turn arithmetic is exact given that number:

           degrees = counts * 0.15 * (sensitivity * 0.6 + 0.2) ** 3 * 8

       so there is nothing to estimate. This is the authoritative source.

    2. What was MEASURED from real turns. The slider gives the magnitude but
       cannot say which direction each axis runs — that depends on the
       operating system's mouse convention meeting Minecraft's angle
       convention, two layers apart, and nothing reports it. So the sign is
       observed, and a machine that turns the other way produces a negative
       scale that the arithmetic then divides by correctly.

    3. A default guess, for when neither is available. It is a guess and it
       is documented as one; it was hardcoded once and overshot fivefold for
       anyone with their sensitivity turned up.

CLOSED LOOP, NOT OPEN
    Nothing here assumes a delta achieved what it asked for. A caller sends a
    bounded correction, observes the real rotation, and asks again. The
    correction is damped below 1.0 on purpose: a feedback loop running at one
    observation per step rings if its gain estimate is even slightly high,
    and undershooting converges where overshooting does not.

NO INPUT, EVER
    This module computes numbers. It cannot press anything: there is no
    controller here, no backend, and the deltas it returns are validated
    again by `action_spec` before they reach any device.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# ── Minecraft's own numbers ──────────────────────────────────────────────────

EYE_HEIGHT = 1.62
"""Where a standing player's eyes are above their feet.

`WorldState.position` is the feet. Aiming from the feet at a block two above
you points the crosshair over its top, which is how an agent stands in front
of a tree and mines the air behind it."""

DEFAULT_PIXELS_PER_DEGREE = 8.0
"""The fallback when the game has not told us and nothing has been measured.

A GUESS, AND LABELLED ONE. It corresponds to a sensitivity slider around 47%.
At 100% the true figure is 1.63, so this asks for five times too much
movement — which is exactly what it did, in a real game, before the mod
started reporting the slider."""

MIN_PIXELS_PER_DEGREE = 0.5
MAX_PIXELS_PER_DEGREE = 60.0

DAMPING = 0.85
"""How much of the computed correction to actually send.

Slightly under one, deliberately. See the module docstring: a loop that always
asks for the whole remaining error rings when its gain estimate is high, and
ringing looks like forty consecutive `look` steps at the same log."""

MIN_MEASURABLE_DEGREES = 3.0
MIN_MEASURABLE_PIXELS = 40.0
"""Below these a reading measures rounding, not sensitivity. The rotation is
reported to a few decimals and the mouse moves in whole pixels."""

MAX_PITCH = 90.0
"""Minecraft clamps the camera here. Asking to look past it is asking for a
correction that can never be achieved, so a target is clamped before it
becomes a delta."""

REACH = 4.5
"""How far a player can touch a block in survival. Aiming at something further
away produces a perfect crosshair and no interaction at all."""


def pixels_per_degree_at(sensitivity) -> float | None:
    """Minecraft's own arithmetic, from the sensitivity slider (0..1).

    Returns None for anything that is not a usable slider position, so the
    caller falls back to measuring rather than trusting a bad reading."""
    try:
        slider = float(sensitivity)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= slider <= 1.0:
        return None
    degrees_per_count = 0.15 * (slider * 0.6 + 0.2) ** 3 * 8.0
    if degrees_per_count <= 1e-6:
        return None
    return 1.0 / degrees_per_count


# ── Angles ───────────────────────────────────────────────────────────────────
#
# Minecraft's yaw is 0 at south (+Z) and increases clockwise seen from above:
# 90 is west (-X), 180 north (-Z), 270 east (+X). Pitch is NEGATIVE looking up
# and positive looking down, which is the opposite of what most people expect
# and worth stating once here rather than rediscovering per caller.

def yaw_to(origin, target) -> float:
    """The yaw that faces from `origin` towards `target`."""
    try:
        dx = target[0] - origin[0]
        dz = target[-1] - origin[-1]
    except (TypeError, IndexError):
        return 0.0
    return math.degrees(math.atan2(-dx, dz))


def yaw_difference(current: float, desired: float) -> float:
    """Shortest signed turn between two yaws, in degrees.

    Wrapped to (-180, 180] so turning from 179 to -179 is two degrees rather
    than three hundred and fifty-eight."""
    return (desired - current + 180.0) % 360.0 - 180.0


def pitch_to(origin, target, eye_height: float = EYE_HEIGHT) -> float:
    """The pitch that looks from `origin` (feet) at `target` (a point)."""
    try:
        dx = target[0] - origin[0]
        dy = target[1] - (origin[1] + eye_height)
        dz = target[2] - origin[2]
    except (TypeError, IndexError):
        return 0.0
    flat = math.hypot(dx, dz)
    if flat < 1e-6:
        return -MAX_PITCH if dy > 0 else MAX_PITCH
    pitch = math.degrees(math.atan2(-dy, flat))
    return max(-MAX_PITCH, min(MAX_PITCH, pitch))


def distance_to(origin, target) -> float:
    try:
        return math.dist(tuple(origin)[:3], tuple(target)[:3])
    except Exception:
        return float("inf")


# ── Where on a block to aim ──────────────────────────────────────────────────

FACE_NORMALS = {
    "north": (0.0, 0.0, -1.0),
    "south": (0.0, 0.0, 1.0),
    "west":  (-1.0, 0.0, 0.0),
    "east":  (1.0, 0.0, 0.0),
    "up":    (0.0, 1.0, 0.0),
    "down":  (0.0, -1.0, 0.0),
}


def target_point(block, player_position, face: str | None = None) -> tuple:
    """A point ON the block that the player can actually see.

    WHY NOT JUST THE CENTRE
        The centre of a block is inside it. That is fine when you are looking
        at it from across a clearing and wrong when you are standing against
        it: aiming at the centre of the block you are touching points the
        crosshair through its far side, and Minecraft may well decide you are
        looking at whatever is behind it.

        So the aim point is pulled a little way out of the block along the
        face turned towards the player. When the face is known (the bridge
        reports it for whatever is under the crosshair) that face is used;
        otherwise the dominant axis from the player to the block picks one."""
    try:
        centre = (float(block[0]) + 0.5, float(block[1]) + 0.5,
                  float(block[2]) + 0.5)
    except (TypeError, IndexError, ValueError):
        return (0.0, 0.0, 0.0)

    normal = FACE_NORMALS.get(str(face or "").strip().lower())
    if normal is None:
        try:
            dx = player_position[0] - centre[0]
            dy = (player_position[1] + EYE_HEIGHT) - centre[1]
            dz = player_position[2] - centre[2]
        except (TypeError, IndexError):
            return centre
        biggest = max(abs(dx), abs(dy), abs(dz))
        if biggest < 1e-6:
            return centre
        if abs(dx) == biggest:
            normal = (1.0 if dx > 0 else -1.0, 0.0, 0.0)
        elif abs(dy) == biggest:
            normal = (0.0, 1.0 if dy > 0 else -1.0, 0.0)
        else:
            normal = (0.0, 0.0, 1.0 if dz > 0 else -1.0)

    # Just outside the surface: far enough that the ray hits this block and
    # not a neighbour, close enough that it is unambiguously this block.
    offset = 0.45
    return (centre[0] + normal[0] * offset,
            centre[1] + normal[1] * offset,
            centre[2] + normal[2] * offset)


# ── The controller ───────────────────────────────────────────────────────────

@dataclass
class MouseAim:
    """Everything that knows how the camera moves, in one object.

    Instantiated per session rather than as a module global, because a global
    is a fact about the process rather than about the game and the two got out
    of step in the tests. `SHARED` below is the one the app uses."""

    sensitivity: float | None = None
    yaw_scale: float | None = None        # signed pixels per degree
    pitch_scale: float | None = None      # signed pixels per degree
    damping: float = DAMPING
    max_delta: int = 400                  # mirrors action_spec's clamp

    _from_settings: float | None = field(default=None, repr=False)

    # ── what it knows ────────────────────────────────────────────────────

    def use_sensitivity(self, sensitivity) -> float | None:
        """Adopt the game's slider. Returns the resolved scale, or None."""
        resolved = pixels_per_degree_at(sensitivity)
        if resolved is not None:
            self.sensitivity = float(sensitivity)
            self._from_settings = resolved
        return resolved

    @property
    def known_from_game(self) -> bool:
        return self._from_settings is not None

    def pixels_per_degree(self) -> float:
        """Magnitude only, for anything that sweeps a fixed amount."""
        if self._from_settings is not None:
            return self._from_settings
        if self.yaw_scale is not None:
            return abs(self.yaw_scale)
        return DEFAULT_PIXELS_PER_DEGREE

    def _scale(self, measured) -> float:
        """The signed scale for one axis.

        The setting gives the magnitude exactly; only the DIRECTION still has
        to be observed, and a measurement that disagrees about direction is
        believed, because that is the part the setting cannot tell us."""
        if self._from_settings is None:
            return measured if measured is not None else DEFAULT_PIXELS_PER_DEGREE
        if measured is not None and measured < 0:
            return -self._from_settings
        return self._from_settings

    # ── learning ─────────────────────────────────────────────────────────

    def observe(self, sent_dx, sent_dy, before_rotation, after_rotation) -> None:
        """Learn from one look: how far each axis moved, and which way."""
        try:
            yaw_before = float(before_rotation[0])
            pitch_before = float(before_rotation[1])
            yaw_after = float(after_rotation[0])
            pitch_after = float(after_rotation[1])
        except (TypeError, IndexError, ValueError):
            return
        self.yaw_scale = _blend(self.yaw_scale, sent_dx,
                                yaw_difference(yaw_before, yaw_after))
        self.pitch_scale = _blend(self.pitch_scale, sent_dy,
                                  pitch_after - pitch_before)

    def reset(self) -> None:
        self.sensitivity = None
        self.yaw_scale = None
        self.pitch_scale = None
        self._from_settings = None

    # ── producing deltas ─────────────────────────────────────────────────

    def delta_for_yaw(self, current_yaw: float, desired_yaw: float) -> int:
        """Mouse dx that turns towards a heading. Undamped: a heading is a
        coarse thing and a navigation turn does not need to settle."""
        return self._clamp(round(yaw_difference(current_yaw, desired_yaw)
                                 * self._scale(self.yaw_scale)))

    def delta_for(self, rotation, desired_yaw: float,
                  desired_pitch: float) -> tuple:
        """(dx, dy, error_degrees) to reach a rotation from where we are.

        The error is what a caller checks against a tolerance — there is no
        point spending a step on a two degree correction the game does not
        care about. It is the error BEFORE this delta, not after: nothing here
        knows what the delta will actually achieve, which is the whole reason
        the loop observes again."""
        try:
            yaw_now = float(rotation[0])
            pitch_now = float(rotation[1])
        except (TypeError, IndexError, ValueError):
            return 0, 0, 180.0

        wanted_pitch = max(-MAX_PITCH, min(MAX_PITCH, float(desired_pitch)))
        dyaw = yaw_difference(yaw_now, desired_yaw)
        dpitch = wanted_pitch - pitch_now

        dx = self._clamp(round(dyaw * self._scale(self.yaw_scale)
                               * self.damping))
        dy = self._clamp(round(dpitch * self._scale(self.pitch_scale)
                               * self.damping))
        return dx, dy, math.hypot(dyaw, dpitch)

    def aim_at(self, position, rotation, target, face: str | None = None) -> tuple:
        """(dx, dy, error) that points the crosshair at a block.

        `target` is a block coordinate; the aim point is chosen on the face
        turned towards the player rather than the block's centre."""
        point = target_point(target, position, face)
        return self.delta_for(rotation,
                              yaw_to(position, point),
                              pitch_to(position, point))

    def within_reach(self, position, target) -> bool:
        """Can the player actually touch it from here?

        A perfect crosshair on something six blocks away does nothing at all,
        and reporting that as an aiming failure sends everyone looking in the
        wrong place."""
        return distance_to((position[0], position[1] + EYE_HEIGHT,
                            position[2]),
                           target_point(target, position)) <= REACH

    def _clamp(self, value) -> int:
        try:
            value = int(value)
        except (TypeError, ValueError):
            return 0
        return max(-self.max_delta, min(value, self.max_delta))

    # ── reporting ────────────────────────────────────────────────────────

    def as_dict(self) -> dict:
        return {
            "sensitivity": self.sensitivity,
            "from_game_settings": self._from_settings,
            "yaw_px_per_degree": self.yaw_scale,
            "pitch_px_per_degree": self.pitch_scale,
            "in_use": self.pixels_per_degree(),
            "default": DEFAULT_PIXELS_PER_DEGREE,
        }

    def describe(self) -> str:
        if self._from_settings is not None:
            head = (f"{self._from_settings:.2f} px/degree, from the game's "
                    f"sensitivity slider ({self.sensitivity:.2f})")
        elif self.yaw_scale is not None:
            head = (f"{abs(self.yaw_scale):.2f} px/degree, measured from your "
                    f"own turns")
        else:
            head = (f"{DEFAULT_PIXELS_PER_DEGREE:.2f} px/degree — a GUESS. "
                    f"The bridge mod has not reported your sensitivity and "
                    f"nothing has been measured yet")
        inverted = [name for name, scale in (("yaw", self.yaw_scale),
                                             ("pitch", self.pitch_scale))
                    if scale is not None and scale < 0]
        if inverted:
            head += f" ({' and '.join(inverted)} inverted on this machine)"
        return head


def _blend(current, pixels, degrees):
    """One axis' new estimate, or the old one when the reading is useless."""
    try:
        pixels, degrees = float(pixels), float(degrees)
    except (TypeError, ValueError):
        return current
    if abs(degrees) < MIN_MEASURABLE_DEGREES or abs(pixels) < MIN_MEASURABLE_PIXELS:
        return current
    measured = pixels / degrees                     # signed, deliberately
    if not MIN_PIXELS_PER_DEGREE <= abs(measured) <= MAX_PIXELS_PER_DEGREE:
        return current
    if current is None or (current > 0) != (measured > 0):
        # First reading, or the direction disagrees with what we thought.
        # Believe the measurement outright rather than averaging towards a
        # sign that is wrong -- half way between +8 and -8 is zero.
        return measured
    return current * 0.5 + measured * 0.5


SHARED = MouseAim()
"""The instance the running assistant uses.

One machine, one mouse, one sensitivity slider — so the knowledge is shared
rather than rebuilt per task. Tests construct their own, or call `reset()`."""


__all__ = [
    "MouseAim", "SHARED", "pixels_per_degree_at", "target_point",
    "yaw_to", "yaw_difference", "pitch_to", "distance_to",
    "EYE_HEIGHT", "DEFAULT_PIXELS_PER_DEGREE", "DAMPING", "MAX_PITCH",
    "REACH", "FACE_NORMALS",
]
