"""
minecraft/gui.py — when a click inside an inventory screen may happen, and
how the pointer is brought to a slot.

Pure rules over what the bridge reports (docs/minecraft/gui.md): the open
screen's kind, the game mode, each slot's centre in window pixels, the
pointer in the same coordinates, the mobs around and the player's health.
Nothing here presses anything; the controller asks `click_refusal` from a
fresh reading immediately before every click, and presses nothing unless it
answers None.

Only heapq, math and dataclasses may be imported here, like navigation.py:
a rule that could reach the input layer would not be a rule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

ALLOWED_SCREENS = frozenset({"inventory", "crafting_table"})
"""The only screens ever clicked: the survival player inventory (with its
2x2 grid) and the crafting table. Never a chest, a furnace, the pause
screen, the creative inventory or anything else."""

CLICKABLE_MODES = frozenset({"survival", "adventure"})
"""Never in creative -- a click there can delete items -- or spectator."""

SLOT_HALF_UNITS = 8.0
"""A slot is 16 GUI units square."""

SLOT_INSET_UNITS = 1.0
"""The rect a click must land in is shrunk by this on each side: never on a
slot's border, so never between two slots."""

WINDOW_MARGIN_PX = 8.0
GUI_HOSTILE_RADIUS = 8.0
GUI_HEALTH_DROP = 1.0
GUI_MAX_AGE_S = 0.5

MAX_GUI_STEP_PX = 400
"""Largest pointer move in one action, like `look`'s bound."""

MAX_CORRECTIONS = 8
"""Pointer moves allowed to reach one slot before giving up and closing."""

MAX_GUI_CLICKS = 64
"""Clicks one task may make."""


def slot_by_index(state, index):
    """The reported slot `index`, or None."""
    for slot in (getattr(state, "slots", None) or ()):
        if slot.i == index:
            return slot
    return None


def slot_rect(slot, scale) -> tuple:
    """(left, top, right, bottom) in window pixels a click on `slot` must
    land inside."""
    half = (SLOT_HALF_UNITS - SLOT_INSET_UNITS) * scale
    return (slot.x - half, slot.y - half, slot.x + half, slot.y + half)


def inside(state, slot) -> bool:
    """Is the reported pointer inside `slot`'s (shrunken) rect?"""
    view = getattr(state, "gui", None)
    if view is None or slot is None:
        return False
    left, top, right, bottom = slot_rect(slot, view.scale)
    x, y = view.cursor_px
    return left <= x <= right and top <= y <= bottom


def screen_refusal(state) -> str | None:
    """Why no click may happen in this screen at all, or None."""
    kind = getattr(state, "screen", None)
    if getattr(state, "gui", None) is None or kind is None:
        return ("I cannot see an inventory screen: no screen is open, or the "
                "bridge mod is too old to report one.")
    if kind not in ALLOWED_SCREENS:
        return (f"The open screen is {' '.join(str(kind).split('_'))}, and I "
                f"only ever click in the inventory or a crafting table.")
    mode = getattr(state, "game_mode", None)
    if mode not in CLICKABLE_MODES:
        return (f"The game mode is {mode or 'unknown'}; I only click in "
                f"screens in survival or adventure.")
    return None


def _nearest_hostile(state):
    near = [e for e in (getattr(state, "nearby_entities", None) or ())
            if getattr(e, "hostile", None) is True
            and getattr(e, "distance", None) is not None]
    return min(near, key=lambda e: e.distance) if near else None


def danger_refusal(state, health_floor=None) -> str | None:
    """A hostile close by, or health lost since the screen was opened."""
    mob = _nearest_hostile(state)
    if mob is not None and mob.distance <= GUI_HOSTILE_RADIUS:
        name = " ".join(str(mob.name or "hostile mob").split(":")[-1]
                        .split("_"))
        return f"a {name} is {mob.distance:.0f} blocks away"
    health = getattr(state, "health", None)
    if health_floor is not None and isinstance(health, (int, float)) \
            and health <= health_floor - GUI_HEALTH_DROP:
        return (f"health went from {health_floor:.0f} to {health:.0f} with "
                f"the screen open")
    return None


def click_refusal(state, slot_index, health_floor=None) -> str | None:
    """Why a click on slot `slot_index` may not happen now, or None.

    Every rule, from one reading: an allowed screen and game mode; a slot
    the game reports; the pointer inside that slot's shrunken rect (which
    is also why a click can never land between slots and drop the carried
    stack); the pointer inside the window with a margin; no hostile near and
    no health lost; and a reading fresh enough to believe. Focus is the
    controller's own guard."""
    problem = screen_refusal(state)
    if problem:
        return problem
    slot = slot_by_index(state, slot_index)
    if slot is None:
        return f"the game does not report a slot {slot_index} in this screen"
    view = state.gui
    x, y = view.cursor_px
    width, height = view.window_px
    if not (WINDOW_MARGIN_PX <= x <= width - WINDOW_MARGIN_PX
            and WINDOW_MARGIN_PX <= y <= height - WINDOW_MARGIN_PX):
        return "the pointer is at the edge of the window"
    if not inside(state, slot):
        return (f"the pointer is not over slot {slot_index} (it is at "
                f"{x:.0f}, {y:.0f}; the slot is at {slot.x:.0f}, "
                f"{slot.y:.0f})")
    danger = danger_refusal(state, health_floor)
    if danger:
        return danger
    age = getattr(state, "age_seconds", None)
    if isinstance(age, (int, float)) and age > GUI_MAX_AGE_S:
        return f"the reading is {age:.1f}s old"
    return None


def _clamp(value: float) -> int:
    return int(max(-MAX_GUI_STEP_PX, min(MAX_GUI_STEP_PX, round(value))))


_SIZE_STEPS = (16, 64, 160)
"""Move sizes (pixels, the larger axis) at which the gain is learned
separately. Acceleration makes a long move go proportionally further than
a short one, so one gain for every size overshoots the long moves and
undershoots the short ones."""


def _size_class(pixels: float) -> int:
    return sum(1 for step in _SIZE_STEPS if abs(pixels) > step)


@dataclass
class Pointer:
    """Brings the pointer to one slot after another, closed loop.

    Each move is a relative delta; the next reading says where the pointer
    really went, and the gain -- window pixels per pixel sent -- is
    measured from that, per size of move, and smoothed. It is not assumed:
    inside a screen the OS applies pointer acceleration, so a long move
    goes proportionally further than a short one."""

    corrections: int = 0
    gains: dict = None            # size class -> [gain x, gain y]

    @property
    def gave_up(self) -> bool:
        return self.corrections >= MAX_CORRECTIONS

    def aim_at(self, slot) -> None:
        """A new target: a fresh budget of corrections, the gains kept."""
        self.corrections = 0

    def _gain(self, size_class: int, axis: int) -> float:
        """The gain learned for this size of move, or the nearest size's,
        or 1.0 before anything is known."""
        gains = self.gains or {}
        known = sorted(gains, key=lambda k: abs(k - size_class))
        return gains[known[0]][axis] if known else 1.0

    def _send_for(self, error: float, axis: int) -> int:
        """Pixels to send to travel `error`, under the gain for the size of
        move that will take -- found by refining once."""
        send = error / self._gain(_size_class(error), axis)
        send = error / self._gain(_size_class(send), axis)
        return _clamp(send)

    def next_move(self, state, slot):
        """(dx, dy) to send, or None: arrived, or out of corrections (the
        caller tells which with `inside`)."""
        if inside(state, slot) or self.gave_up:
            return None
        cx, cy = state.gui.cursor_px
        ex, ey = slot.x - cx, slot.y - cy
        dx, dy = self._send_for(ex, 0), self._send_for(ey, 1)
        # Never a zero move while still outside: at least one pixel.
        if dx == 0 and abs(ex) >= 0.5:
            dx = 1 if ex > 0 else -1
        if dy == 0 and abs(ey) >= 0.5:
            dy = 1 if ey > 0 else -1
        self.corrections += 1
        return dx, dy

    def observe(self, sent, before, after) -> None:
        """Learn the gain from one move: where the pointer went for what was
        sent, filed under the size of the move. Small or vanishing moves
        teach nothing and are skipped."""
        if self.gains is None:
            self.gains = {}
        size_class = _size_class(max(abs(sent[0]), abs(sent[1])))
        current = self.gains.get(size_class)
        for axis in (0, 1):
            if abs(sent[axis]) < 3:
                continue
            measured = (after[axis] - before[axis]) / sent[axis]
            if measured <= 0.05 or math.isnan(measured):
                continue
            measured = max(0.2, min(5.0, measured))
            if current is None:
                current = [measured, measured]
                self.gains[size_class] = current
            else:
                current[axis] = 0.3 * current[axis] + 0.7 * measured


__all__ = ["ALLOWED_SCREENS", "CLICKABLE_MODES", "MAX_CORRECTIONS",
           "MAX_GUI_CLICKS", "MAX_GUI_STEP_PX", "GUI_HOSTILE_RADIUS",
           "Pointer", "click_refusal", "danger_refusal", "inside",
           "screen_refusal", "slot_by_index", "slot_rect"]
