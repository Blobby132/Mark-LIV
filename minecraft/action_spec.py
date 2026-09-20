"""
minecraft/action_spec.py — the vocabulary, and its limits.

WHY A VOCABULARY RATHER THAN A KEYBOARD
    The planner does not get to say "press these keys for this long". It picks
    from a list of named actions with bounded parameters, and this module is
    where a request becomes a validated spec or an `InvalidAction` — before
    anything reaches the ledger.

    So "move forward for 30 seconds" does not become a 30-second key hold. It
    becomes a 2-second one with `clamped=True` and the original value kept in
    the result, so the planner can see it did not get what it asked for and
    decide whether to ask again.

CLAMP, AND SAY SO
    Refusing an over-long request would be defensible, but a planner that asks
    for 3 seconds and gets an error learns nothing and usually retries with
    3 seconds. Clamping to the limit and reporting both numbers gets the useful
    part of the action done AND tells the truth about what happened, which is
    what the verification step downstream actually needs.

    The limit itself is not negotiable: there is no parameter, environment
    variable or session option that raises it.
"""

from __future__ import annotations

from dataclasses import dataclass

from minecraft.errors import InvalidAction

# ── The limits ───────────────────────────────────────────────────────────────

MAX_MOVE_DURATION_S = 2.0
"""Two seconds of held movement. Long enough to cross a few blocks, short
enough that a mistake is a step rather than a journey — and short enough that
the focus check, which runs on every tick, gets many chances to catch a
problem."""

MIN_MOVE_DURATION_S = 0.05

MAX_LOOK_DELTA_PX = 400
"""Per call, in each axis. Roughly a quarter turn at default sensitivity,
though the real relationship is unknown until calibration — which is why the
parameter is pixels and not degrees."""

JUMP_TAP_S = 0.08
"""One tap. Not a parameter: a held jump key is just sustained jumping, and
nothing in this phase needs it."""

MOVE_KEYS = {
    "forward": "w",
    "back":    "s",
    "left":    "a",
    "right":   "d",
}

DIRECTIONS = tuple(sorted(MOVE_KEYS))


# ── Specs ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MoveSpec:
    direction: str
    duration: float
    requested_duration: float
    key: str

    @property
    def clamped(self) -> bool:
        return abs(self.duration - self.requested_duration) > 1e-9

    def as_dict(self) -> dict:
        return {"direction": self.direction, "duration": self.duration,
                "requested_duration": self.requested_duration}


@dataclass(frozen=True)
class LookSpec:
    dx: int
    dy: int
    requested_dx: int
    requested_dy: int

    @property
    def clamped(self) -> bool:
        return self.dx != self.requested_dx or self.dy != self.requested_dy

    def as_dict(self) -> dict:
        return {"dx": self.dx, "dy": self.dy,
                "requested_dx": self.requested_dx,
                "requested_dy": self.requested_dy}


@dataclass(frozen=True)
class JumpSpec:
    duration: float = JUMP_TAP_S
    key: str = "space"
    clamped: bool = False

    def as_dict(self) -> dict:
        return {"duration": self.duration}


# ── Parsing ──────────────────────────────────────────────────────────────────

def _as_float(value, field: str) -> float:
    if isinstance(value, bool):
        raise InvalidAction(f"'{field}' must be a number, not true/false.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise InvalidAction(f"'{field}' must be a number, not "
                            f"{type(value).__name__}.")
    if number != number or number in (float("inf"), float("-inf")):
        raise InvalidAction(f"'{field}' must be an ordinary number.")
    return number


def _as_int(value, field: str) -> int:
    if isinstance(value, bool):
        raise InvalidAction(f"'{field}' must be a number, not true/false.")
    try:
        return int(_as_float(value, field))
    except InvalidAction:
        raise


def parse_move(params: dict) -> MoveSpec:
    raw_direction = str((params or {}).get("direction", "")).strip().lower()
    if not raw_direction:
        raise InvalidAction(
            f"Which way? One of: {', '.join(DIRECTIONS)}."
        )
    if raw_direction not in MOVE_KEYS:
        raise InvalidAction(
            f"'{raw_direction}' is not a direction I can walk. "
            f"One of: {', '.join(DIRECTIONS)}."
        )

    requested = _as_float((params or {}).get("duration", 0.5), "duration")
    if requested <= 0:
        raise InvalidAction("'duration' must be greater than zero seconds.")

    duration = max(MIN_MOVE_DURATION_S, min(requested, MAX_MOVE_DURATION_S))
    return MoveSpec(direction=raw_direction, duration=duration,
                    requested_duration=requested,
                    key=MOVE_KEYS[raw_direction])


def parse_look(params: dict) -> LookSpec:
    params = params or {}
    if "dx" not in params and "dy" not in params:
        raise InvalidAction(
            "Looking needs dx and/or dy, in pixels of mouse movement. "
            "Degrees are not available yet — the relationship between pixels "
            "and in-game degrees depends on your mouse sensitivity and has "
            "not been calibrated."
        )
    for forbidden in ("yaw", "pitch", "degrees", "angle"):
        if forbidden in params:
            raise InvalidAction(
                f"'{forbidden}' is not supported. I can only move the mouse by "
                f"a number of pixels (dx, dy); I do not yet know how many "
                f"pixels make a degree in your game."
            )

    requested_dx = _as_int(params.get("dx", 0), "dx")
    requested_dy = _as_int(params.get("dy", 0), "dy")
    if requested_dx == 0 and requested_dy == 0:
        raise InvalidAction("dx and dy are both zero — that would do nothing.")

    def _clamp(value: int) -> int:
        return max(-MAX_LOOK_DELTA_PX, min(value, MAX_LOOK_DELTA_PX))

    return LookSpec(dx=_clamp(requested_dx), dy=_clamp(requested_dy),
                    requested_dx=requested_dx, requested_dy=requested_dy)


def parse_jump(params: dict) -> JumpSpec:
    """Takes no parameters on purpose.

    A `duration` here would be a held jump, which is not a thing this phase
    needs and would be one more bounded value to police."""
    for unexpected in ("duration", "height", "count", "times"):
        if unexpected in (params or {}):
            raise InvalidAction(
                f"Jump takes no '{unexpected}'. It is a single hop; ask again "
                f"for another one."
            )
    return JumpSpec()


def limits() -> dict:
    """The numbers, for a status report and for the tool description, so the
    model is told the bounds rather than discovering them by being refused."""
    return {
        "max_move_duration_s": MAX_MOVE_DURATION_S,
        "min_move_duration_s": MIN_MOVE_DURATION_S,
        "max_look_delta_px": MAX_LOOK_DELTA_PX,
        "jump_tap_s": JUMP_TAP_S,
        "directions": list(DIRECTIONS),
    }


__all__ = [
    "MoveSpec", "LookSpec", "JumpSpec",
    "parse_move", "parse_look", "parse_jump", "limits",
    "MAX_MOVE_DURATION_S", "MIN_MOVE_DURATION_S", "MAX_LOOK_DELTA_PX",
    "JUMP_TAP_S", "MOVE_KEYS", "DIRECTIONS",
]
