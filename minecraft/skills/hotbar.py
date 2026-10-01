"""
minecraft/skills/hotbar.py -- HotbarFetch: moving an item from the main
inventory into the hotbar through the inventory screen, and which hotbar
slot to fill. Shared by mining (collect), eat_food and place_block_at.
Moved here unchanged from skills.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from minecraft import verification as verify_mod
from minecraft import mining as mining_mod
from minecraft import gui as gui_mod
from minecraft.task_runner import MAX_TASK_STEPS, Step

from minecraft.skills.base import (
    FOODS, UNSAFE_FOODS, _learn_pointer, _slot_of, _words,
)


# ── Into the hotbar ──────────────────────────────────────────────────────────

FETCH_WORTH_S = 1.5
"""Seconds a tool in the main inventory must save on each block before a
mining task goes into the inventory for it -- about what the trip costs:
open, two or three pointer moves, the swap, close. A tool the hotbar has no
substitute for (stone, and nothing in the hotbar that drops it) is always
worth it."""

_KEEP_AT_HAND = frozenset({"bow", "crossbow", "shield", "trident", "torch",
                           "flint_and_steel", "fishing_rod", "ender_pearl",
                           "totem_of_undying", "shears"})


def _worth_keeping(name) -> bool:
    """A tool, a weapon, food, a bucket or the like: what a player keeps on
    the hotbar on purpose. A stack of blocks is not."""
    name = str(name or "").split(":")[-1]
    return (mining_mod.tool_from_item(name).kind is not None
            or name in FOODS or name in UNSAFE_FOODS
            or name in _KEEP_AT_HAND or name.endswith("_bucket"))


def hotbar_slot_to_fill(state) -> int:
    """The hotbar slot (0-8) a fetched stack goes into: the first empty
    one; else the last holding nothing worth keeping at hand; else the last
    that is not the one held. Whatever was there is swapped into the slot
    the fetched stack came from -- nothing is dropped."""
    held = getattr(state, "selected_slot", None)
    by_slot = {s.slot: s.name for s in (getattr(state, "inventory", None) or ())
               if s.slot is not None and 0 <= s.slot <= 8
               and (s.count or 0) > 0}
    for slot in range(9):
        if slot not in by_slot:
            return slot
    for slot in range(8, -1, -1):
        if slot != held and not _worth_keeping(by_slot[slot]):
            return slot
    return 8 if held != 8 else 7


@dataclass
class HotbarFetch:
    """Bring one stack from the main inventory into the hotbar, the way a
    player does: open the inventory, put the pointer on the stack, press the
    hotbar slot's number key -- the game swaps the two -- and close it.

    Not a task of its own: eat_food and the mining tasks run it when what
    they need is in the main inventory, and go on once it is done. One
    swap, never a stack on the pointer, so nothing can be dropped. The swap
    passes the controller's click gate (minecraft/gui.py) like every click,
    and is checked against the slots the game reports, then against the
    inventory once the screen is shut.

    While the screen is open it keeps craft_item's watch -- a hostile within
    8 blocks or any health lost closes the screen and stops it -- and its
    owner hands it the runner's watch for that time (`in_screen`), because
    the runner would stop the task with the screen left open. With
    `hurt_is_expected` (eating while starving loses health on its own) a
    trip stopped by health alone, with no hostile near, is made once more.
    """

    item: str = ""
    hotbar: int = 0                  # 0-8
    hurt_is_expected: bool = False

    _phase: str = "start"            # start, opening, point, closing, done
    _reason: str = ""
    _landed: bool = False
    _pointer: object = field(default_factory=gui_mod.Pointer)
    _peak: float | None = None
    _open_tries: int = 0
    _close_tries: int = 0
    _still: int = 0
    _swapped: bool = False
    _again: bool = False
    _retried: bool = False
    _source: int | None = None       # the menu slot the stack is taken from
    _displaced: str | None = None

    @property
    def in_screen(self) -> bool:
        """From the step that opens the inventory until it is seen shut."""
        return self._phase in ("opening", "point", "closing")

    @property
    def finished(self) -> bool:
        return self._phase == "done"

    @property
    def landed(self) -> bool:
        return self.finished and self._landed

    @property
    def failed(self) -> bool:
        return self.finished and not self._landed

    @property
    def done_reason(self) -> str:
        return self._reason

    def plan(self, state, step_index: int = 0, history: tuple = ()):
        last = history[-1] if history else None
        _learn_pointer(self._pointer, last)
        if self._phase == "done":
            return None
        if self._phase == "closing":
            return self._closing(state)
        health = getattr(state, "health", None)
        if isinstance(health, (int, float)) and (
                self._peak is None or health > self._peak):
            self._peak = float(health)
        if self._phase == "start":
            return self._start(state)
        danger = gui_mod.danger_refusal(state, self._peak)
        if danger:
            mob = gui_mod.danger_refusal(state)
            if mob is None and self.hurt_is_expected and not self._retried:
                self._retried = self._again = True
            return self._abort(state, danger)
        if step_index >= MAX_TASK_STEPS - 2 and state.screen:
            return self._abort(state, "I ran out of steps")
        if self._phase == "opening":
            if state.screen == "inventory":
                self._phase = "point"
            elif state.screen:
                return self._abort(state, f"a {_words(state.screen)} screen "
                                          f"opened, not the inventory")
            elif self._open_tries >= 2:
                return self._stop("the inventory would not open")
            else:
                return self._open_step()
        return self._point(state, last)

    def refusal(self, state) -> str | None:
        """Why the trip cannot start, before anything is pressed."""
        mode = getattr(state, "game_mode", None)
        if mode is None:
            return ("the bridge mod does not report the game's screens -- "
                    "run install_mod.bat with Minecraft closed for a version "
                    "that does")
        if mode not in gui_mod.CLICKABLE_MODES:
            return (f"the game is in {mode} mode, and I only click in screens "
                    f"in survival or adventure")
        if getattr(state, "screen", None):
            return f"a {_words(state.screen)} screen is already open"
        danger = gui_mod.danger_refusal(state)
        if danger:
            return f"I did not open the inventory: {danger}"
        return None

    def _start(self, state):
        problem = self.refusal(state)
        if problem:
            return self._stop(problem)
        self._phase = "opening"
        return self._open_step()

    def _open_step(self):
        self._open_tries += 1
        return Step(action="inventory", params={"state": "open"},
                    expectation=verify_mod.screen_is("inventory"),
                    note=f"open the inventory to move the {_words(self.item)} "
                         f"into hotbar slot {self.hotbar + 1}")

    def _stop(self, reason):
        self._reason = reason
        self._phase = "done"
        return None

    def _abort(self, state, reason):
        self._reason = reason
        self._landed = False
        if not getattr(state, "screen", None):
            return self._closed(state)
        self._phase = "closing"
        return self._closing(state)

    def _closing(self, state):
        if not getattr(state, "screen", None):
            return self._closed(state)
        if self._close_tries >= 2:
            self._landed = False
            self._again = False
            return self._stop(f"{self._reason}; the inventory would not "
                              f"close".lstrip("; "))
        self._close_tries += 1
        return Step(action="inventory", params={"state": "close"},
                    expectation=verify_mod.screen_closed(),
                    note="close the inventory")

    def _closed(self, state):
        """Shut: the verdict from the inventory itself -- or, after a trip
        stopped only by expected damage, the second trip."""
        if self._again:
            self._again = False
            self._phase = "start"
            self._peak = None
            self._open_tries = self._close_tries = self._still = 0
            self._source = None
            self._pointer.aim_at(None)
            return self._start(state)
        self._phase = "done"
        if not self._landed:
            return None
        here = next((s for s in (state.inventory or ())
                     if s.slot == self.hotbar and (s.count or 0) > 0), None)
        if here is None or here.name != self.item:
            self._landed = False
            self._reason = (f"the swap looked right in the screen, but with it "
                            f"shut hotbar slot {self.hotbar + 1} holds "
                            f"{_words(here.name) if here else 'nothing'}")
            return None
        self._reason = (f"moved the {_words(self.item)} into hotbar slot "
                        f"{self.hotbar + 1}")
        if self._displaced:
            self._reason += (f" (the {_words(self._displaced)} there went "
                             f"where it came from)")
        return None

    def _point(self, state, last):
        if state.screen != "inventory":
            return self._abort(state, "the inventory closed under me")
        if last is not None and last.step.get("action") == "gui_point":
            if last.verification.get("status") == verify_mod.FAILED:
                self._still += 1
            else:
                self._still = 0
            if self._still >= 2:
                return self._abort(state, "the pointer did not move when I "
                                          "moved the mouse, twice")
        if self._swapped:
            self._swapped = False
            if last is None or last.step.get("action") != "gui_swap":
                return self._abort(state, "I lost track of the swap")
            if last.verification.get("status") != verify_mod.SUCCESS:
                why = (last.action_result.get("error")
                       or last.verification.get("reason") or "no reason")
                return self._abort(state, f"the swap did not do what it "
                                          f"should: {why}")
            self._landed = True
            self._phase = "closing"
            return self._closing(state)
        slots = state.slots or ()
        row = sorted((s for s in slots if s.role == "hotbar"),
                     key=lambda s: s.i)
        if len(row) != 9:
            return self._abort(state, "the screen does not show the hotbar "
                                      "where I expect it")
        target = row[self.hotbar]
        source = _slot_of(state, self._source) if self._source is not None \
            else None
        if source is None or source.item != self.item:
            stacks = [s for s in slots if s.role == "inventory"
                      and s.item == self.item and (s.count or 0) > 0]
            if not stacks:
                return self._abort(state, f"I cannot see the "
                                          f"{_words(self.item)} in the "
                                          f"inventory screen")
            source = max(stacks, key=lambda s: (s.count, -s.i))
            self._source = source.i
            self._pointer.aim_at(source)
        if not gui_mod.inside(state, source):
            move = self._pointer.next_move(state, source)
            if move is None:
                return self._abort(state, (
                    f"the pointer would not settle on the "
                    f"{_words(self.item)} after {gui_mod.MAX_CORRECTIONS} "
                    f"corrections"))
            return Step(action="gui_point",
                        params={"dx": move[0], "dy": move[1]},
                        expectation=verify_mod.pointer_moved(),
                        note=f"point at the {_words(self.item)}")
        moved = (source.item, source.count)
        was = (target.item, target.count) if target.item else None
        source_i, target_i = source.i, target.i

        def swapped(before, after):
            there = _slot_of(after, target_i)
            back = _slot_of(after, source_i)
            now_there = (there.item, there.count) if there and there.item \
                else None
            now_back = (back.item, back.count) if back and back.item \
                else None
            if now_there == moved and now_back == was:
                return True, (f"the {_words(moved[0])} is in hotbar slot "
                              f"{self.hotbar + 1}.")
            return False, (f"hotbar slot {self.hotbar + 1} holds "
                           f"{now_there or 'nothing'} and the stack's old "
                           f"slot holds {now_back or 'nothing'}.")
        self._swapped = True
        self._displaced = target.item
        return Step(action="gui_swap",
                    params={"slot": source.i, "hotbar": self.hotbar + 1},
                    expectation=verify_mod.gui_effect(
                        f"swap the {_words(self.item)} into hotbar slot "
                        f"{self.hotbar + 1}", swapped),
                    note=f"press {self.hotbar + 1} over the "
                         f"{_words(self.item)}: it swaps into hotbar slot "
                         f"{self.hotbar + 1}")
