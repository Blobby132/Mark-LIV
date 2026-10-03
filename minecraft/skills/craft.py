"""
minecraft/skills/craft.py -- craft_item: the inventory grid or a crafting
table, one verified click at a time. Moved here unchanged from skills.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from minecraft import action_spec, navigation as nav, verification as verify_mod
from minecraft import aiming as aiming_mod
from minecraft import gui as gui_mod
from minecraft import building as building_mod
from minecraft import recipes as recipes_mod
from minecraft.state import NearbyBlock
from minecraft.task_runner import MAX_TASK_STEPS, Step

from minecraft.skills.base import (
    _crosshair_at, _interactive, _learn_pointer, _slot_of, _words,
)
from minecraft.skills.navigate import NavigateTo
from minecraft.skills.place_build import PlaceBlockAt


# ── Crafting ─────────────────────────────────────────────────────────────────

_CLICK_ACTIONS = ("gui_click", "gui_swap")


def _carried_of(state):
    stack = getattr(state, "carried", None)
    if stack is None or not getattr(stack, "name", None):
        return None
    return stack.name, int(stack.count or 0)


def _held_name(state):
    """The held item's name, "" for an empty hand, None when unknown."""
    held = getattr(state, "held_item", None)
    if held is not None and getattr(held, "name", None):
        return building_mod.short(held.name)
    if getattr(state, "inventory", None) is None \
            or getattr(state, "selected_slot", None) is None:
        return None
    for stack in state.inventory:
        if stack.slot == state.selected_slot and (stack.count or 0) > 0:
            return building_mod.short(stack.name)
    return ""


def _no_game_mode(state) -> str:
    """Why craft_item cannot start without game_mode, naming the field. Three
    different causes, and only one of them is fixed by reinstalling."""
    lacks = getattr(state, "lacks", None)
    if callable(lacks) and lacks("gui"):
        return ("I cannot craft: the installed mod is older than this Jarvis "
                "and does not report game_mode or the open screen, which "
                "crafting needs. Quit Minecraft, run install_mod.bat, then "
                "start Minecraft again.")
    if getattr(state, "features", None) is not None:
        return ("I cannot craft yet: this reading from the bridge mod had no "
                "game_mode, so I cannot tell survival from creative. Try "
                "again in a moment.")
    return ("I cannot craft: nothing I can read reports game_mode or the "
            "open screen -- that needs the bridge mod running in the game.")


@dataclass
class CraftItem:
    """craft_item: make `count` of `item` by clicking in the crafting grid.

    The 2x2 grid in the inventory, or a crafting table's 3x3 within reach.
    Each ingredient: the pointer brought onto its stack (closed loop on the
    pointer the game reports), a left click to pick the stack up, a right
    click per cell to put ONE in, a left click to put the rest back -- then
    a shift-click on the output, which with one of everything in the grid
    makes exactly one batch. Every click goes through the controller's gate
    (minecraft/gui.py) and every result is checked against the slots the
    game reports before the next; anything unexpected closes the screen and
    says what happened. The proof at the end is the inventory: the output
    up, the ingredients down.

    Its own danger watch, stricter than the runner's (a hostile within 8
    blocks, any health lost) -- the runner's would stop the task with the
    screen open; this one closes the screen first. See docs/minecraft/gui.md.
    """

    item: str = ""
    count: int = 1

    name = "craft_item"
    verifiable_with = ("inventory", "slots", "screen")
    watch_hostiles = False
    watch_health = False

    _phase: str = "check"
    _reason: str = ""
    _succeeded: bool = False
    _recipe: object = None
    _grid: int = 2
    _batches: int = 0
    _made: int = 0
    _start: dict = field(default_factory=dict)
    _ops: list = field(default_factory=list)
    _clicked: bool = False
    _pointer: object = field(default_factory=gui_mod.Pointer)
    _clicks: int = 0
    _peak: float | None = None
    _open_tries: int = 0
    _close_tries: int = 0
    _aim_tries: int = 0
    _table: object = None
    _walker: object = None
    _aborted: bool = False
    _still: int = 0              # pointer moves in a row that did not move it
    _placer: object = None
    _table_note: str = ""
    _hand_before: int | None = None    # the slot changed to open a table
    _hand_tries: int = 0
    _hand_restored: bool = False

    @property
    def goal(self) -> str:
        return f"craft {self.count} {_words(self.item)}"

    @property
    def failed(self) -> bool:
        return not self._succeeded

    @property
    def done_reason(self) -> str:
        reason = self._reason or "stopped before crafting anything"
        return f"{self._table_note}; {reason}" if self._table_note else reason

    # ── the loop ─────────────────────────────────────────────────────────

    def plan(self, state, step_index: int, history: tuple):
        step = self._plan_step(state, step_index, history)
        if step is None and self._phase == "done":
            # However it ended: the slot changed to open a table goes back.
            return self._restore_hand(state)
        return step

    def _plan_step(self, state, step_index: int, history: tuple):
        last = history[-1] if history else None
        self._learn(last)
        if self._phase == "done":
            return None
        if self._phase == "closing":
            return self._closing(state)

        health = getattr(state, "health", None)
        if isinstance(health, (int, float)) and (
                self._peak is None or health > self._peak):
            self._peak = float(health)
        danger = gui_mod.danger_refusal(state, self._peak)
        if danger:
            return self._abort(state, f"I stopped crafting: {danger}.",
                               stash=False)
        # Keep the last steps for closing the screen: a task cut off by the
        # step limit would leave it open with the grid half full.
        if step_index >= MAX_TASK_STEPS - 3 and state.screen:
            return self._abort(state, "I ran out of steps before finishing.")

        if self._phase == "check":
            return self._check(state)
        if self._phase == "placing":
            return self._placing(state, step_index, history)
        if self._phase == "open":
            return self._open(state, history)
        return self._craft(state, last)

    def _stop(self, reason):
        self._reason = reason
        self._phase = "done"
        return None

    def _abort(self, state, reason, stash=True):
        """Stop: put away anything on the pointer if that is safe, close
        the screen, and say why."""
        self._aborted = True
        self._reason = reason
        self._ops = []
        self._clicked = False
        if not state.screen:
            self._phase = "done"
            return None
        if stash and _carried_of(state) is not None:
            free = self._free_slot(state)
            if free is not None:
                self._ops = [self._stash_op(state, free)]
                self._phase = "stashing"
                self._pointer.aim_at(None)
                return self._next_op_step(state)
        self._phase = "closing"
        return self._closing(state)

    def _closing(self, state):
        if not state.screen:
            self._phase = "done"
            self._finish(state)
            return None
        if self._close_tries >= 2:
            self._phase = "done"
            self._reason += " The screen would not close."
            return None
        self._close_tries += 1
        return Step(action="inventory", params={"state": "close"},
                    expectation=verify_mod.screen_closed(),
                    note="close the screen")

    def _finish(self, state):
        """The verdict, from the inventory: what the output and the
        ingredients did between the start and now."""
        if not self._start:
            return
        output = self._recipe.output
        before = self._start.get(output, 0)
        after = verify_mod._count_of(state, {output})
        gained = after - before
        used = []
        for tag in recipes_mod.ingredients(self._recipe):
            names = recipes_mod.TAGS.get(tag, frozenset({tag}))
            was = self._start.get(("tag", tag), 0)
            now = verify_mod._count_of(state, names)
            if now != was:
                used.append(f"{_words(tag)} {was} → {now}")
        proof = (f"counted in the inventory: {output} {before} → {after}"
                 + (f"; {', '.join(used)}" if used else ""))
        if not self._aborted and gained >= self.count:
            self._succeeded = True
            self._reason = f"crafted {gained} {_words(output)} ({proof})."
        elif gained > 0:
            self._reason = (f"{self._reason} I crafted {gained} of "
                            f"{self.count} {_words(output)} before that "
                            f"({proof}).").strip()
        else:
            self._reason = (f"{self._reason} Nothing was crafted "
                            f"({proof}).").strip()

    # ── before anything ──────────────────────────────────────────────────

    def _check(self, state):
        recipe = recipes_mod.recipe_for(self.item)
        if recipe is None:
            return self._stop(
                f"I do not know how to craft {self.item!r}. I can craft: "
                f"{', '.join(sorted(recipes_mod.RECIPES))}.")
        self._recipe = recipe
        mode = getattr(state, "game_mode", None)
        if mode is None:
            return self._stop(_no_game_mode(state))
        if mode not in gui_mod.CLICKABLE_MODES:
            return self._stop(f"The game is in {mode} mode, and I only craft "
                              f"in survival or adventure.")
        if state.inventory is None:
            return self._stop("I cannot see the inventory, so I cannot tell "
                              "what there is to craft with.")
        self.count = max(1, int(self.count or 1))
        self._batches = -(-self.count // recipe.count)
        short = []
        for tag, per_batch in recipes_mod.ingredients(recipe).items():
            names = recipes_mod.TAGS.get(tag, frozenset({tag}))
            have = verify_mod._count_of(state, names)
            need = per_batch * self._batches
            if have < need:
                short.append(f"{need} {_words(tag)} (I have {have})")
            self._start[("tag", tag)] = have
        if short:
            return self._stop(f"To craft {self.count} {_words(recipe.output)}"
                              f" I need {', '.join(short)}.")
        self._start[recipe.output] = verify_mod._count_of(
            state, {recipe.output})
        self._grid = 2 if recipes_mod.fits(recipe, 2) else 3
        if self._grid == 3 and state.screen != "crafting_table":
            tables = nav.blocks_matching(state, {"crafting_table"})
            table = nav.nearest_of(state, tables, reachable_only=True) \
                or nav.nearest_of(state, [
                    t for t in tables if aiming_mod.SHARED.within_reach(
                        state.position, t.position)])
            if table is None:
                if verify_mod._count_of(state, {"crafting_table"}) > 0:
                    return self._place_table(state)
                return self._stop(
                    f"A {_words(recipe.output)} needs a crafting table's 3x3 "
                    f"grid, and I cannot see a crafting table I can get to. "
                    f"Place one near me (craft_item crafting_table makes "
                    f"one) and ask again.")
            self._table = table
        self._phase = "open"
        return self._open(state, ())

    # ── the hand that right-clicks the table ─────────────────────────────

    def _safe_hand(self, state):
        """A hotbar_select to a slot `interact` will right-click with --
        an empty one first, else one holding nothing on the deny-list --
        when the held item is on it (an axe, after chopping). None when
        the hand is fine already. Stops when there is no such slot."""
        held = _held_name(state)
        if held is None or not action_spec.interact_refusal(held):
            return None
        by_slot = {s.slot: s.name for s in (state.inventory or ())
                   if s.slot is not None and 0 <= s.slot <= 8
                   and (s.count or 0) > 0}
        safe = [slot for slot in range(9) if slot not in by_slot] + [
            slot for slot in range(9) if slot in by_slot
            and not action_spec.interact_refusal(by_slot[slot])]
        if not safe or self._hand_tries >= 2:
            return self._stop(
                f"I would right-click the crafting table holding "
                f"{_words(held)}, which could use it instead, and no hotbar "
                f"slot is empty or holds something safe to right-click "
                f"with.")
        self._hand_tries += 1
        if self._hand_before is None:
            self._hand_before = state.selected_slot
        slot = safe[0] + 1
        return Step(action="hotbar_select", params={"slot": slot},
                    expectation=verify_mod.holding_slot(slot),
                    note=f"hold hotbar slot {slot} rather than the "
                         f"{_words(held)} to open the crafting table")

    def _restore_hand(self, state):
        """Put back the slot changed for the table, once."""
        if self._hand_before is None or self._hand_restored \
                or state.selected_slot == self._hand_before \
                or getattr(state, "screen", None):
            return None
        self._hand_restored = True
        slot = self._hand_before + 1
        return Step(action="hotbar_select", params={"slot": slot},
                    expectation=verify_mod.holding_slot(slot),
                    note=f"back to hotbar slot {slot}")

    # ── a table of its own ───────────────────────────────────────────────

    def _place_table(self, state):
        """No table in reach and one in the inventory: put it down beside
        the player with place_block_at -- the same proof as any placement --
        and use it."""
        local = nav.LocalMap.from_state(state)
        here = (math.floor(state.position[0]), math.floor(state.position[2]))
        spots = []
        for dx in range(-2, 3):
            for dz in range(-2, 3):
                column = (here[0] + dx, here[1] + dz)
                if column == here or max(abs(dx), abs(dz)) > 2:
                    continue
                ground = local.ground_at(*column)
                if ground is None:
                    continue
                cell = (column[0], ground + 1, column[1])
                verdict, _name = building_mod.what_is_at(local, state, cell)
                if verdict not in (building_mod.EMPTY, building_mod.REPLACE):
                    continue
                if not building_mod.references(local, state, cell,
                                               unusable=_interactive):
                    continue
                spots.append((abs(dx) + abs(dz), cell))
        if not spots:
            return self._stop(
                f"A {_words(self._recipe.output)} needs a crafting table, and "
                f"there is no clear spot beside me to put down the one I "
                f"have.")
        cell = min(spots)[1]
        self._placer = PlaceBlockAt(x=cell[0], y=cell[1], z=cell[2],
                                    item="crafting_table",
                                    allowed=frozenset({"crafting_table"}))
        self._phase = "placing"
        return self._placing(state, 0, ())

    def _placing(self, state, step_index, history):
        step = self._placer.plan(state, step_index, history)
        if step is not None:
            return step
        if not self._placer.placed:
            return self._stop(f"I could not put down the crafting table: "
                              f"{self._placer.done_reason}")
        cell = self._placer.cell
        self._table_note = (f"placed the crafting table at {cell} "
                            f"({self._placer.done_reason.split(': ', 1)[-1]})")
        self._table = NearbyBlock(*cell, "crafting_table", True)
        self._phase = "open"
        return self._open(state, history)

    # ── opening the grid ─────────────────────────────────────────────────

    def _open(self, state, history):
        wanted = "inventory" if self._grid == 2 else "crafting_table"
        if state.screen == wanted:
            self._phase = "craft"
            return self._craft(state, None)
        if state.screen:
            return self._abort(state, f"A {_words(state.screen)} screen is "
                                      f"open, not the one I need.")
        if self._open_tries >= 2:
            return self._stop(f"The {_words(wanted)} would not open.")
        if self._grid == 2:
            self._open_tries += 1
            return Step(action="inventory", params={"state": "open"},
                        expectation=verify_mod.screen_is("inventory"),
                        note="open the inventory (its 2x2 crafting grid)")
        table = self._table
        if not aiming_mod.SHARED.within_reach(state.position, table.position):
            if self._walker is None:
                column = nav.approach_column(state, table)
                if column is None:
                    return self._stop("There is nowhere to stand within "
                                      "reach of the crafting table.")
                self._walker = NavigateTo(destination=column)
            step = self._walker.plan(state, 0, history)
            if step is not None:
                return step
            if self._walker.failed:
                return self._stop(f"I could not get to the crafting table: "
                                  f"{self._walker.done_reason}")
            self._walker = None
            if not aiming_mod.SHARED.within_reach(state.position,
                                                  table.position):
                return self._stop("The crafting table is out of reach from "
                                  "where I can stand.")
        seen = _crosshair_at(state)
        if seen is not None and tuple(seen[0]) == tuple(table.position) \
                and seen[1] == "crafting_table":
            hand = self._safe_hand(state)
            if hand is not None:
                return hand
            self._open_tries += 1
            return Step(action="interact", params={},
                        expectation=verify_mod.screen_is("crafting_table"),
                        note=f"use the crafting table at {table.position}")
        if self._aim_tries >= 8:
            return self._stop("I could not get the crosshair onto the "
                              "crafting table.")
        self._aim_tries += 1
        dx, dy, error = nav.aim_at(state.position, state.rotation,
                                   table.position)
        return Step(action="look", params={"dx": dx, "dy": dy},
                    expectation=verify_mod.turned(min_degrees=0.5),
                    note=f"aim at the crafting table ({error:.1f}° off)")

    # ── the clicks ───────────────────────────────────────────────────────

    def _learn(self, last):
        _learn_pointer(self._pointer, last)

    def _craft(self, state, last):
        if state.screen not in gui_mod.ALLOWED_SCREENS:
            return self._abort(state, "The screen closed under me.",
                               stash=False)
        if last is not None and last.step.get("action") == "gui_point":
            if last.verification.get("status") == verify_mod.FAILED:
                self._still += 1
            else:
                self._still = 0
            # Two moves that went nowhere: the pointer is not following.
            # Stop now, closing the screen -- not after the runner's own
            # stuck check, which would end the task with the screen open.
            if self._still >= 2:
                return self._abort(state, "The pointer did not move when I "
                                          "moved the mouse, twice.")
        if self._clicked:
            self._clicked = False
            if last is None or last.step.get("action") not in _CLICK_ACTIONS:
                return self._abort(state, "I lost track of my last click.")
            if last.verification.get("status") != verify_mod.SUCCESS:
                why = (last.action_result.get("error")
                       or last.verification.get("reason") or "no reason")
                return self._abort(state, f"A click did not do what it "
                                          f"should: {why}")
            op = self._ops.pop(0)
            if not self._ops or self._ops[0]["slot"] != op["slot"]:
                self._pointer.aim_at(None)
            if op["kind"] == "output":
                self._made += op["batches"]
        if not self._ops:
            if self._phase == "stashing":
                self._phase = "closing"
                return self._closing(state)
            if self._made >= self._batches:
                self._phase = "closing"
                return self._closing(state)
            built = self._build_ops(state)
            if isinstance(built, str):
                return self._abort(state, built)
            self._ops = built
        return self._next_op_step(state)

    def _next_op_step(self, state):
        op = self._ops[0]
        slot = _slot_of(state, op["slot"])
        if slot is None:
            return self._abort(state, f"The game no longer reports slot "
                                      f"{op['slot']}.")
        if op["kind"] == "output" and slot.item != self._recipe.output:
            return self._abort(state, (
                f"The grid should make {_words(self._recipe.output)} but the "
                f"output shows {_words(slot.item or 'nothing')}."))
        if not gui_mod.inside(state, slot):
            move = self._pointer.next_move(state, slot)
            if move is None:
                return self._abort(state, (
                    f"The pointer would not settle on the {op['what']} after "
                    f"{gui_mod.MAX_CORRECTIONS} corrections."))
            return Step(action="gui_point",
                        params={"dx": move[0], "dy": move[1]},
                        expectation=verify_mod.pointer_moved(),
                        note=f"point at the {op['what']}")
        if self._clicks >= gui_mod.MAX_GUI_CLICKS:
            return self._abort(state, f"I reached the limit of "
                                      f"{gui_mod.MAX_GUI_CLICKS} clicks.")
        self._clicks += 1
        self._clicked = True
        return Step(action="gui_click", params=op["params"],
                    expectation=op["expect"], note=op["note"])

    def _free_slot(self, state):
        for slot in (state.slots or ()):
            if slot.role in ("inventory", "hotbar") and slot.item is None:
                return slot
        return None

    def _stash_op(self, state, free):
        held = _carried_of(state)

        def check(before, after):
            if _carried_of(after) is None:
                return True, "nothing is left on the pointer."
            return False, "a stack is still on the pointer."
        return {"kind": "stash", "slot": free.i, "what": "empty slot",
                "params": {"slot": free.i, "button": "left"},
                "expect": verify_mod.gui_effect("put the stack away", check),
                "note": f"put the {_words(held[0])} on the pointer into an "
                        f"empty slot"}

    def _build_ops(self, state):
        """The clicks for one batch, from what the screen shows now -- or a
        sentence saying why there is no safe way to do it."""
        slots = state.slots or ()
        cells = sorted((s for s in slots if s.role == "craft_in"),
                       key=lambda s: s.i)
        output = next((s for s in slots if s.role == "craft_out"), None)
        if len(cells) != self._grid * self._grid or output is None:
            return "The crafting grid is not the shape I expected."
        busy = [s for s in cells if s.item]
        if busy:
            return (f"The crafting grid already has {_words(busy[0].item)} in "
                    f"it; I will not mix my recipe with what is there.")
        # Every remaining batch at once: that many in each cell, and one
        # shift-click on the output crafts exactly as many batches as the
        # grid holds. A cell holds 64.
        batches = min(self._batches - self._made, 64)
        wanted = {}
        for r, row in enumerate(self._recipe.pattern):
            for c, tag in enumerate(row):
                if tag is not None:
                    wanted.setdefault(tag, []).extend(
                        [cells[r * self._grid + c]] * batches)
        ops = []
        filled = {}
        for tag, demand in wanted.items():
            names = recipes_mod.TAGS.get(tag, frozenset({tag}))
            stacks = sorted((s for s in slots
                             if s.role in ("inventory", "hotbar")
                             and s.item in names and s.count > 0),
                            key=lambda s: -s.count)
            left = list(demand)
            for source in stacks:
                if not left:
                    break
                take = left[:source.count]
                left = left[source.count:]
                ops.extend(self._ingredient_ops(source, take, filled))
            if left:
                return f"I ran out of {_words(tag)} part-way."
        ops.append(self._output_op(output, batches))
        return ops

    def _ingredient_ops(self, source, cells, filled):
        """Pick up `source`, put one in each of `cells` (a cell repeated
        gets more than one), put the rest back. `filled` counts what each
        cell will hold, across stacks."""
        item, total = source.item, source.count

        def picked(before, after):
            slot = _slot_of(after, source.i)
            if _carried_of(after) == (item, total) and \
                    (slot is None or slot.item is None):
                return True, f"picked up {total} {_words(item)}."
            return False, (f"the pointer holds {_carried_of(after)}, not "
                           f"{total} {_words(item)}.")
        ops = [{"kind": "pickup", "slot": source.i, "what": f"{_words(item)} "
                f"stack", "params": {"slot": source.i, "button": "left"},
                "expect": verify_mod.gui_effect(f"pick up the "
                                                f"{_words(item)}", picked),
                "note": f"pick up the {_words(item)}"}]
        remaining = total
        for cell in cells:
            expected_left = remaining - 1
            filled[cell.i] = filled.get(cell.i, 0) + 1
            expected_in = filled[cell.i]

            def placed(before, after, cell=cell, expected_left=expected_left,
                       expected_in=expected_in):
                slot = _slot_of(after, cell.i)
                carried = _carried_of(after)
                left = carried[1] if carried and carried[0] == item else 0
                if slot is not None and slot.item == item \
                        and slot.count == expected_in \
                        and left == expected_left:
                    return True, (f"{expected_in} {_words(item)} in that "
                                  f"cell.")
                return False, (f"the cell holds {slot.count if slot else 0} "
                               f"{_words(slot.item or 'nothing') if slot else ''}"
                               f" and {left} are left on the pointer.")
            ops.append({"kind": "place", "slot": cell.i,
                        "what": "crafting cell",
                        "params": {"slot": cell.i, "button": "right"},
                        "expect": verify_mod.gui_effect(
                            f"put one {_words(item)} in the grid", placed),
                        "note": f"one {_words(item)} into the grid"})
            remaining = expected_left
        if remaining > 0:
            def returned(before, after, left=remaining):
                slot = _slot_of(after, source.i)
                if _carried_of(after) is None and slot is not None and \
                        slot.item == item and slot.count == left:
                    return True, f"put {left} {_words(item)} back."
                return False, "the rest did not go back where it came from."
            ops.append({"kind": "putback", "slot": source.i,
                        "what": f"{_words(item)}'s slot",
                        "params": {"slot": source.i, "button": "left"},
                        "expect": verify_mod.gui_effect(
                            f"put the rest of the {_words(item)} back",
                            returned),
                        "note": f"put the rest of the {_words(item)} back"})
        return ops

    def _output_op(self, output, batches):
        made, per = self._recipe.output, self._recipe.count * batches

        def crafted(before, after):
            gained = verify_mod._count_of(after, {made}) \
                - verify_mod._count_of(before, {made})
            if gained >= per:
                return True, f"{gained} {_words(made)} into the inventory."
            return False, (f"{gained} {_words(made)} reached the inventory, "
                           f"not {per}.")
        return {"kind": "output", "slot": output.i, "what": "output slot",
                "batches": batches,
                "params": {"slot": output.i, "button": "left", "shift": True},
                "expect": verify_mod.Expectation(
                    name="crafted", goal=f"take the {_words(made)}",
                    fields=("inventory",), predicate=crafted),
                "note": f"take the {_words(made)} (shift-click: {batches} "
                        f"batch{'es' if batches != 1 else ''})"}
