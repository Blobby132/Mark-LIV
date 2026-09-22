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

MAX_ATTACK_DURATION_S = 2.0
"""One swing-and-hold, not a mining session. Breaking an oak log by hand takes
about three seconds, so a single attack deliberately does NOT finish the job:
the task runner repeats bounded attacks and re-checks the target between them.
That is the difference between "I held the button" and "the block broke", and
building it in at the limit is what makes the distinction unavoidable."""

MAX_USE_DURATION_S = 2.0
"""Placing, eating, drawing a bow. Same bound as attack for the same reason."""

MAX_SNEAK_DURATION_S = MAX_MOVE_DURATION_S
MAX_SPRINT_DURATION_S = MAX_MOVE_DURATION_S

MAX_MINE_DURATION_S = 10.0
"""How long one mining action may hold the button.

WHY THIS IS NOT 2.0 LIKE EVERYTHING ELSE
    It was, and mining did not work. Minecraft resets block-breaking progress
    the moment the button comes up, so repeated short swings do not
    accumulate damage -- each one starts from zero. Breaking an oak log by
    hand takes about three seconds of CONTINUOUS holding, and stone with a
    pickaxe longer.

    So mining is the one action whose bound is set by the game rather than by
    caution. Ten seconds covers hand-breaking most early-game blocks; it does
    not cover obsidian, which is a limitation rather than an oversight.

    The hold is not usually spent. With a state source attached the mine loop
    watches the target and releases the instant it changes, so a log that
    breaks at 3.1s costs 3.1 seconds. The bound is what happens when nothing
    can see the block -- and then it IS spent, which is why it is not
    thirty."""

DEFAULT_MINE_DURATION_S = 4.0
"""Long enough for wood by hand, short enough to notice a mistake."""

MAX_INTERACT_DURATION_S = 1.0
"""Right-click on a door, chest or crafting table. Short: these are taps, and
a held right-click on a stack of blocks places a wall of them."""

MAX_EAT_DURATION_S = 2.0
"""Eating holds right-click for about 1.6 seconds in modern versions."""

PLACE_TAP_S = 0.08
"""Placing is a tap. A hold places repeatedly as the crosshair drifts, which
is how an agent asked for one block builds a staircase."""

DROP_TAP_S = 0.08
INVENTORY_TAP_S = 0.08

INVENTORY_KEY = "e"
DROP_KEY = "q"
CLOSE_KEY = "esc"

HOTBAR_SLOTS = tuple(range(1, 10))
"""1-9 as the player sees them. Slot 0 does not exist on a Minecraft hotbar,
and `selected_slot` in WorldState is 0-8 because that is what the game's own
data uses -- the conversion happens here, once, rather than in a planner."""

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

SPACE_KEY = "space"
SNEAK_KEY = "shift"
SPRINT_KEY = "ctrl"

ATTACK_BUTTON = "left"
USE_BUTTON = "right"


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
class HopSpec:
    """Walking forward and jumping at the same time.

    WHY THIS IS ONE ACTION AND NOT TWO
        Getting onto a one-block ledge in Minecraft means holding forward
        THROUGH the jump. Walk, stop, jump, walk lands you back where you
        started: you rise, and with no horizontal momentum you come straight
        back down on the same block. That is why an agent that did it in
        three separate steps needed a person to say "jump" and then still did
        not get up.

        It is a fixed pair — one movement key and the jump key — not an
        arbitrary key combination. There is no field here that names a key,
        so this cannot become a way to press something else.
    """

    direction: str
    duration: float
    requested_duration: float
    key: str
    jump_key: str = SPACE_KEY

    @property
    def keys(self) -> tuple:
        return (self.key, self.jump_key)

    @property
    def clamped(self) -> bool:
        return abs(self.duration - self.requested_duration) > 1e-9

    def as_dict(self) -> dict:
        return {"direction": self.direction, "duration": self.duration,
                "requested_duration": self.requested_duration,
                "jumping": True}


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


@dataclass(frozen=True)
class HoldSpec:
    """A bounded hold of any combination of keys and mouse buttons.

    One shape for attack, use_item, sneak and sprint, because from the
    controller's point of view they differ only in what goes down and for how
    long -- and a single hold path means a single release path."""

    action: str
    keys: tuple = ()
    buttons: tuple = ()
    duration: float = 0.0
    requested_duration: float = 0.0
    detail: dict = None

    @property
    def clamped(self) -> bool:
        return abs(self.duration - self.requested_duration) > 1e-9

    def as_dict(self) -> dict:
        out = {"duration": self.duration,
               "requested_duration": self.requested_duration}
        if self.detail:
            out.update(self.detail)
        return out


@dataclass(frozen=True)
class HotbarSpec:
    """Selecting a hotbar slot. A tap, not a hold -- there is no duration to
    bound, so the only validation is that the slot exists."""

    slot: int
    key: str
    clamped: bool = False

    def as_dict(self) -> dict:
        return {"slot": self.slot}


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


MAX_HOP_DURATION_S = 1.0
"""A hop is short on purpose.

Long enough to carry you onto the block in front, short enough that being
wrong about the obstacle costs a step rather than a journey — and short enough
that the focus guard, which runs every tick, gets many chances during it."""


def parse_move_and_jump(params: dict) -> HopSpec:
    """Forward (or another direction) while jumping, briefly.

    Reuses the movement direction table, so the set of things this can press
    is exactly the set `move` can press, plus the jump key. Nothing here
    accepts a key name."""
    params = params or {}
    direction = str(params.get("direction", "forward")).lower().strip()
    if direction not in MOVE_KEYS:
        raise InvalidAction(
            f"'{direction}' is not a direction I can jump in. "
            f"One of: {', '.join(DIRECTIONS)}."
        )
    duration, requested = _bounded_duration(params, JUMP_TAP_S + 0.3,
                                            MAX_HOP_DURATION_S)
    return HopSpec(direction=direction, duration=duration,
                   requested_duration=requested, key=MOVE_KEYS[direction])


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


def _bounded_duration(params: dict, default: float, maximum: float) -> tuple:
    """Shared duration handling: validate, clamp, and keep what was asked.

    Returns (duration, requested). Clamping rather than refusing, for the
    reason in the module docstring -- but the requested value survives so the
    caller can tell the planner it did not get what it asked for."""
    requested = _as_float((params or {}).get("duration", default), "duration")
    if requested <= 0:
        raise InvalidAction("'duration' must be greater than zero seconds.")
    return max(MIN_MOVE_DURATION_S, min(requested, maximum)), requested


def _optional_direction(params: dict) -> str:
    """A direction to travel in while sneaking or sprinting, or ''."""
    raw = str((params or {}).get("direction", "")).strip().lower()
    if not raw:
        return ""
    if raw not in MOVE_KEYS:
        raise InvalidAction(
            f"'{raw}' is not a direction I can walk. "
            f"One of: {', '.join(DIRECTIONS)}."
        )
    return raw


def parse_attack(params: dict) -> HoldSpec:
    """Hold the attack button for a bounded time.

    Takes no target. Minecraft attacks whatever is under the crosshair, so
    aiming is a `look` and hitting is this -- keeping them separate means the
    planner has to observe between them, which is where verification lives."""
    for unsupported in ("target", "block", "entity", "at", "position"):
        if unsupported in (params or {}):
            raise InvalidAction(
                f"Attack takes no '{unsupported}'. I hit whatever is under the "
                f"crosshair, so aim first with 'look', check what you are "
                f"looking at with 'read_state', then attack."
            )
    duration, requested = _bounded_duration(params, 0.5, MAX_ATTACK_DURATION_S)
    return HoldSpec(action="attack", buttons=(ATTACK_BUTTON,),
                    duration=duration, requested_duration=requested)


def parse_use_item(params: dict) -> HoldSpec:
    """Hold the use button: place a block, eat, open a door."""
    for unsupported in ("item", "slot", "target", "block"):
        if unsupported in (params or {}):
            raise InvalidAction(
                f"Use takes no '{unsupported}'. It uses whatever is in your "
                f"hand on whatever is under the crosshair -- select the item "
                f"first with 'hotbar_select'."
            )
    duration, requested = _bounded_duration(params, 0.2, MAX_USE_DURATION_S)
    return HoldSpec(action="use_item", buttons=(USE_BUTTON,),
                    duration=duration, requested_duration=requested)


def parse_sneak(params: dict) -> HoldSpec:
    """Hold sneak, optionally while walking.

    Sneaking in place is genuinely useful -- it is what stops you walking off
    an edge -- so `direction` is optional rather than required."""
    direction = _optional_direction(params)
    duration, requested = _bounded_duration(params, 0.5, MAX_SNEAK_DURATION_S)
    keys = (SNEAK_KEY,) + ((MOVE_KEYS[direction],) if direction else ())
    return HoldSpec(action="sneak", keys=keys, duration=duration,
                    requested_duration=requested,
                    detail={"direction": direction or None})


def parse_sprint(params: dict) -> HoldSpec:
    """Hold sprint plus a direction.

    Unlike sneak, the direction is required: sprinting on the spot is not a
    thing Minecraft does, and accepting it would have the planner believe it
    had moved."""
    direction = _optional_direction(params) or "forward"
    duration, requested = _bounded_duration(params, 1.0, MAX_SPRINT_DURATION_S)
    return HoldSpec(action="sprint",
                    keys=(SPRINT_KEY, MOVE_KEYS[direction]),
                    duration=duration, requested_duration=requested,
                    detail={"direction": direction})


def parse_hotbar(params: dict) -> HotbarSpec:
    """Select hotbar slot 1-9. Out of range is refused, not clamped.

    Clamping is right for a duration, where the useful part of the action
    still happens. It is wrong here: asking for slot 12 and silently getting
    slot 9 would have the planner believe it is holding something it is not,
    and every action after that reasons from a false premise."""
    params = params or {}
    if "slot" not in params:
        raise InvalidAction(
            f"Which hotbar slot? {HOTBAR_SLOTS[0]} to {HOTBAR_SLOTS[-1]}."
        )
    slot = _as_int(params.get("slot"), "slot")
    if slot not in HOTBAR_SLOTS:
        raise InvalidAction(
            f"There is no hotbar slot {slot}. The hotbar is "
            f"{HOTBAR_SLOTS[0]} to {HOTBAR_SLOTS[-1]}."
        )
    return HotbarSpec(slot=slot, key=str(slot))


def parse_mine(params: dict) -> HoldSpec:
    """Hold the attack button against a block.

    Physically identical to `attack`; kept separate because the capability and
    the verification differ. A broken block is observable on the F3 overlay; a
    damaged mob is not, so pretending one is the other would make mining look
    unverifiable and combat look verifiable, both wrongly."""
    for unsupported in ("target", "block", "at", "position", "until"):
        if unsupported in (params or {}):
            raise InvalidAction(
                f"Mining takes no '{unsupported}'. I break whatever is under "
                f"the crosshair -- aim with 'look', confirm what you are "
                f"aiming at with 'read_state', then mine."
            )
    duration, requested = _bounded_duration(params, DEFAULT_MINE_DURATION_S,
                                            MAX_MINE_DURATION_S)
    return HoldSpec(action="mine", buttons=(ATTACK_BUTTON,),
                    duration=duration, requested_duration=requested)


def parse_place(params: dict) -> HoldSpec:
    """Place the held block against whatever the crosshair is on.

    A tap, with no duration parameter: a held right-click places block after
    block as the view drifts, and an agent asked for one block would build a
    trail of them."""
    for unsupported in ("duration", "count", "times", "block", "item"):
        if unsupported in (params or {}):
            raise InvalidAction(
                f"Placing takes no '{unsupported}'. It is one tap that places "
                f"whatever is in your hand against the block you are looking "
                f"at -- select the item first with 'hotbar_select', and ask "
                f"again for another block."
            )
    return HoldSpec(action="place", buttons=(USE_BUTTON,),
                    duration=PLACE_TAP_S, requested_duration=PLACE_TAP_S)


def parse_interact(params: dict) -> HoldSpec:
    """Right-click a block or entity: open a door, a chest, a crafting table."""
    duration, requested = _bounded_duration(params, 0.1,
                                            MAX_INTERACT_DURATION_S)
    return HoldSpec(action="interact", buttons=(USE_BUTTON,),
                    duration=duration, requested_duration=requested)


def parse_eat(params: dict) -> HoldSpec:
    """Hold right-click to eat or drink what is held."""
    duration, requested = _bounded_duration(params, 1.8, MAX_EAT_DURATION_S)
    return HoldSpec(action="eat", buttons=(USE_BUTTON,),
                    duration=duration, requested_duration=requested)


def parse_drop(params: dict) -> HoldSpec:
    """Tap Q: drop one of the held item.

    Refuses 'all' explicitly. Ctrl-Q drops a whole stack, and an agent that
    misjudged which slot was selected would empty it in one keystroke -- so
    the stack version is not available and the refusal says why."""
    for unsupported in ("all", "stack", "count", "amount"):
        if unsupported in (params or {}):
            raise InvalidAction(
                "I can only drop one item at a time. Dropping a whole stack "
                "is one keystroke away from emptying a slot I misread, so it "
                "is not available."
            )
    return HoldSpec(action="drop", keys=(DROP_KEY,), duration=DROP_TAP_S,
                    requested_duration=DROP_TAP_S)


def parse_inventory(params: dict) -> HoldSpec:
    """Open or close the inventory.

    Opening uses E and closing uses ESC rather than E again: if the inventory
    is already shut, E opens it, so "close" implemented as E would toggle the
    wrong way exactly when the state was misread. ESC closes and does nothing
    when nothing is open, which fails in the harmless direction."""
    raw = str((params or {}).get("state", "open")).strip().lower()
    if raw in ("open", "opened", "show"):
        key, name = INVENTORY_KEY, "open"
    elif raw in ("close", "closed", "hide", "exit"):
        key, name = CLOSE_KEY, "close"
    else:
        raise InvalidAction(
            f"'{raw}' is not something I can do to the inventory. "
            f"Use state=open or state=close."
        )
    return HoldSpec(action=f"inventory_{name}", keys=(key,),
                    duration=INVENTORY_TAP_S,
                    requested_duration=INVENTORY_TAP_S,
                    detail={"state": name})


def limits() -> dict:
    """The numbers, for a status report and for the tool description, so the
    model is told the bounds rather than discovering them by being refused."""
    return {
        "max_move_duration_s": MAX_MOVE_DURATION_S,
        "min_move_duration_s": MIN_MOVE_DURATION_S,
        "max_look_delta_px": MAX_LOOK_DELTA_PX,
        "jump_tap_s": JUMP_TAP_S,
        "directions": list(DIRECTIONS),
        "max_attack_duration_s": MAX_ATTACK_DURATION_S,
        "max_use_duration_s": MAX_USE_DURATION_S,
        "max_sneak_duration_s": MAX_SNEAK_DURATION_S,
        "max_sprint_duration_s": MAX_SPRINT_DURATION_S,
        "hotbar_slots": list(HOTBAR_SLOTS),
        "max_mine_duration_s": MAX_MINE_DURATION_S,
        "max_interact_duration_s": MAX_INTERACT_DURATION_S,
        "max_eat_duration_s": MAX_EAT_DURATION_S,
        "place_is_a_tap": True,
    }


__all__ = [
    "MoveSpec", "LookSpec", "JumpSpec", "HoldSpec", "HotbarSpec",
    "parse_move", "parse_look", "parse_jump", "parse_move_and_jump",
    "HopSpec", "MAX_HOP_DURATION_S", "parse_attack",
    "parse_use_item", "parse_sneak", "parse_sprint", "parse_hotbar",
    "parse_mine", "parse_place", "parse_interact", "parse_eat", "parse_drop",
    "parse_inventory", "limits",
    "MAX_MOVE_DURATION_S", "MIN_MOVE_DURATION_S", "MAX_LOOK_DELTA_PX",
    "MAX_ATTACK_DURATION_S", "MAX_USE_DURATION_S", "MAX_SNEAK_DURATION_S",
    "MAX_SPRINT_DURATION_S", "HOTBAR_SLOTS", "MAX_MINE_DURATION_S",
    "MAX_INTERACT_DURATION_S", "MAX_EAT_DURATION_S", "PLACE_TAP_S",
    "DEFAULT_MINE_DURATION_S",
    "INVENTORY_KEY", "DROP_KEY", "CLOSE_KEY",
    "JUMP_TAP_S", "MOVE_KEYS", "DIRECTIONS", "SNEAK_KEY", "SPRINT_KEY",
    "ATTACK_BUTTON", "USE_BUTTON",
]
