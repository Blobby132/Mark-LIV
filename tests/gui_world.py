"""
A simulated Minecraft inventory screen, for testing what clicks in one.

TreeWorld's world (walking, a crosshair) plus the screens a player opens:
the survival inventory with its 2x2 grid, and a crafting table's 3x3. It
models what the real game does and the bridge reports:

  - slots laid out as the game lays them out (InventoryMenu, CraftingMenu),
    reported with their centres in window pixels;
  - a POINTER that moves by relative deltas with OS acceleration -- a long
    move goes proportionally further, as it does in a screen;
  - Minecraft's click rules: left picks up or puts down a whole stack (or
    swaps), right picks up half or puts down one, the output slot takes one
    batch (shift: every batch the grid holds, into the inventory), a number
    key swaps the slot under the pointer with that hotbar slot, and closing
    a screen returns the grid and the pointer's stack to the inventory;
  - shaped recipes from minecraft/recipes.py, matched anywhere in the grid;
  - hunger, and eating what the selected hotbar slot holds;
  - THE GATE: every click and swap is judged by minecraft/gui.py's
    click_refusal from a fresh reading, exactly as the controller does, and
    refused the same way.

Crude where crudeness is harmless: one window size, no recipe book, no
stack limits below 64."""

from __future__ import annotations

import time

from minecraft import gui as gui_mod
from minecraft import recipes
from minecraft.controller import ActionResult
from minecraft.state import (EXACT, BlockRef, EntityRef, GuiSlot, GuiView,
                             ItemStack, NearbyBlock, WorldState)
from tests.support.sim_world import TreeWorld, flat

SCALE = 3.0
WINDOW = (1920, 1080)
LEFT = (WINDOW[0] / SCALE - 176) / 2
TOP = (WINDOW[1] / SCALE - 166) / 2


def _inventory_layout(menu_offset_main, menu_offset_hotbar):
    """(menu index, role, container slot, gx, gy) of the player inventory
    part of a menu."""
    out = []
    for row in range(3):
        for col in range(9):
            container = 9 + row * 9 + col
            out.append((menu_offset_main + row * 9 + col, "inventory",
                        container, 8 + col * 18, 84 + row * 18))
    for col in range(9):
        out.append((menu_offset_hotbar + col, "hotbar", col, 8 + col * 18,
                    142))
    return out


INVENTORY_SCREEN = (
    [(0, "craft_out", None, 154, 28)]
    + [(1 + r * 2 + c, "craft_in", None, 98 + c * 18, 18 + r * 18)
       for r in range(2) for c in range(2)]
    + [(5 + r, "armor", 39 - r, 8, 8 + r * 18) for r in range(4)]
    + _inventory_layout(9, 36)
    + [(45, "offhand", 40, 77, 62)]
)
CRAFTING_SCREEN = (
    [(0, "craft_out", None, 124, 35)]
    + [(1 + r * 3 + c, "craft_in", None, 30 + c * 18, 17 + r * 18)
       for r in range(3) for c in range(3)]
    + _inventory_layout(10, 37)
)


def accelerate(pixels):
    """Pointer acceleration, roughly as Windows applies it."""
    return 1.0 + min(pixels, 400) / 250.0


class GuiWorld(TreeWorld):

    def __init__(self, items, selected=0, table=None, mode="survival",
                 mobs=(), health=20.0, pointer_gain=accelerate, hunger=20.0,
                 **kwargs):
        logs = [NearbyBlock(*table, "crafting_table", True)] if table else []
        super().__init__(flat(), logs, **kwargs)
        # TreeWorld keeps its own inventory dict under the name the runner
        # dispatches the `inventory` ACTION to; this world has a method.
        del self.inventory
        self.stacks = {slot: [name, count] for slot, (name, count)
                       in items.items()}            # container slot -> stack
        self.selected = selected
        self.mode = mode
        self.mobs = list(mobs)
        self.health = health
        self.hunger = hunger
        self.eaten = []
        self.pointer_gain = pointer_gain
        self.screen = None
        self.grid = {}                               # cell -> [name, count]
        self.carried = None
        self.cursor = [WINDOW[0] / 2, WINDOW[1] / 2]
        self.clicks = 0
        self.swaps = 0
        self.used_with = []                 # interacts refused for the hand
        self.refused = []
        self.dropped = []
        self.table = table

    # ── what is where ───────────────────────────────────────────────────

    def _layout(self):
        return {"inventory": INVENTORY_SCREEN,
                "crafting_table": CRAFTING_SCREEN}.get(self.screen, ())

    def _grid_size(self):
        return 2 if self.screen == "inventory" else 3

    def _content(self, entry):
        index, role, container, _gx, _gy = entry
        if role == "craft_in":
            return self.grid.get(index - 1)
        if role == "craft_out":
            made = self._match()
            return [made[0], made[1]] if made else None
        return self.stacks.get(container)

    def _set(self, entry, stack):
        index, role, container, _gx, _gy = entry
        if stack is not None and stack[1] <= 0:
            stack = None
        if role == "craft_in":
            if stack is None:
                self.grid.pop(index - 1, None)
            else:
                self.grid[index - 1] = stack
        else:
            if stack is None:
                self.stacks.pop(container, None)
            else:
                self.stacks[container] = stack

    def _match(self):
        """(output, count) the grid makes, or None."""
        size = self._grid_size()
        cells = {(k // size, k % size): v[0] for k, v in self.grid.items()
                 if v and v[1] > 0}
        if not cells:
            return None
        rows = [r for r, _ in cells]
        cols = [c for _, c in cells]
        r0, c0 = min(rows), min(cols)
        shape = {(r - r0, c - c0): name for (r, c), name in cells.items()}
        for recipe in recipes.RECIPES.values():
            wanted = {(r, c): tag for r, row in enumerate(recipe.pattern)
                      for c, tag in enumerate(row) if tag is not None}
            if set(wanted) != set(shape):
                continue
            if all(shape[k] in recipes.TAGS.get(tag, {tag})
                   for k, tag in wanted.items()):
                return recipe.output, recipe.count
        return None

    def _consume_grid(self):
        for k in list(self.grid):
            self.grid[k][1] -= 1
            if self.grid[k][1] <= 0:
                del self.grid[k]

    def _add_to_inventory(self, name, count):
        for slot in list(range(9)) + list(range(9, 36)):
            stack = self.stacks.get(slot)
            if stack and stack[0] == name and stack[1] < 64:
                room = min(64 - stack[1], count)
                stack[1] += room
                count -= room
            if count == 0:
                return 0
        for slot in list(range(9)) + list(range(9, 36)):
            if slot not in self.stacks:
                self.stacks[slot] = [name, min(64, count)]
                count -= min(64, count)
            if count == 0:
                return 0
        return count

    # ── the bridge ──────────────────────────────────────────────────────

    def count(self, name):
        total = sum(s[1] for s in self.stacks.values() if s[0] == name)
        if self.carried and self.carried[0] == name:
            total += self.carried[1]
        return total

    def read(self):
        from tests.support.sim_world import SimWorld
        base = SimWorld.read(self)
        hit = self.crosshair()
        target = (BlockRef(name=hit[1], x=hit[0][0], y=hit[0][1],
                           z=hit[0][2], face=hit[2])
                  if hit else BlockRef(name="air"))
        inventory = tuple(ItemStack(slot=slot, name=name, count=count)
                          for slot, (name, count) in sorted(self.stacks.items())
                          if count > 0)
        held = next((s for s in inventory if s.slot == self.selected), None)
        slots = gui = carried = None
        if self.screen in ("inventory", "crafting_table"):
            slots = []
            for entry in self._layout():
                index, role, _c, gx, gy = entry
                stack = self._content(entry)
                slots.append(GuiSlot(
                    i=index, role=role, x=(LEFT + gx + 8) * SCALE,
                    y=(TOP + gy + 8) * SCALE,
                    item=stack[0] if stack else None,
                    count=stack[1] if stack else 0))
            slots = tuple(slots)
            gui = GuiView(scale=SCALE, window_px=WINDOW,
                          cursor_px=tuple(self.cursor))
            carried = (ItemStack(name=self.carried[0],
                                 count=self.carried[1])
                       if self.carried else None)
        return WorldState(
            position=base.position, rotation=base.rotation,
            surface=base.surface, notable_blocks=tuple(self.logs),
            target_block=target, scan_radius=base.scan_radius,
            inventory=inventory, selected_slot=self.selected, held_item=held,
            health=self.health, hunger=self.hunger,
            nearby_entities=tuple(self.mobs),
            on_ground=True, screen=self.screen, gui=gui, slots=slots,
            carried=carried, game_mode=self.mode, captured_at=time.time(),
            source="bridge", confidence=EXACT)

    # ── the controller ──────────────────────────────────────────────────

    def _done(self, name, params, ok=True, reason="", error=""):
        return ActionResult(ok=ok, action=name, requested=dict(params or {}),
                            actual_duration_ms=80, stopped_reason=reason,
                            error=error)

    def inventory(self, params):
        if params.get("state") == "close":
            if self.screen is None:          # the controller refuses (item 6)
                return self._done("inventory_close", params, ok=False,
                                  reason="no_screen",
                                  error="No screen is open.")
            self._close()
        elif self.screen is None:
            self.screen = "inventory"
        return self._done("inventory", params)

    def _close(self):
        for stack in list(self.grid.values()):
            left = self._add_to_inventory(stack[0], stack[1])
            if left:
                self.dropped.append((stack[0], left))
        self.grid = {}
        if self.carried:
            left = self._add_to_inventory(*self.carried)
            if left:
                self.dropped.append((self.carried[0], left))
            self.carried = None
        self.screen = None

    def interact(self, params):
        # The controller's held-item check (item 3), as it makes it.
        from minecraft import action_spec
        held = self.stacks.get(self.selected)
        refusal = action_spec.interact_refusal(held[0] if held else "")
        if refusal:
            self.used_with.append(held[0])
            return self._done("interact", params, ok=False,
                              reason="held_item", error=refusal)
        hit = self.crosshair()
        if hit and hit[1] == "crafting_table" and self.screen is None:
            self.screen = "crafting_table"
        return self._done("interact", params)

    def hotbar_select(self, params):
        if self.screen is not None:
            return self._done("hotbar_select", params, ok=False,
                              reason="gui_gate", error="a screen is open")
        self.selected = int(params["slot"]) - 1
        return self._done("hotbar_select", params)

    def eat(self, params):
        """Hold right click: the selected stack is eaten if it is food, the
        hold is long enough, the player is hungry and no screen is open."""
        from minecraft import skills
        from minecraft.skills import eat as skills_eat
        held = self.stacks.get(self.selected)
        if self.screen is None and held and held[0] in skills.FOODS \
                and self.hunger < 20 and float(params.get("duration", 0)) \
                >= skills_eat.eat_ticks(held[0]) / 20.0:
            held[1] -= 1
            if held[1] <= 0:
                del self.stacks[self.selected]
            self.hunger = min(20.0, self.hunger + skills.FOODS[held[0]])
            self.eaten.append(held[0])
        return self._done("eat", params)

    def gui_point(self, params):
        refusal = gui_mod.screen_refusal(self.read())
        if refusal:
            self.refused.append(refusal)
            return self._done("gui_point", params, ok=False,
                              reason="gui_gate", error=refusal)
        dx, dy = int(params.get("dx", 0)), int(params.get("dy", 0))
        gain = self.pointer_gain(abs(dx) + abs(dy))
        self.cursor[0] = min(WINDOW[0], max(0.0, self.cursor[0] + dx * gain))
        self.cursor[1] = min(WINDOW[1], max(0.0, self.cursor[1] + dy * gain))
        return self._done("gui_point", params)

    def _slot_entry(self, index):
        return next((e for e in self._layout() if e[0] == index), None)

    def _gate(self, name, params):
        refusal = gui_mod.click_refusal(self.read(), int(params["slot"]))
        if refusal:
            self.refused.append(refusal)
            return self._done(name, params, ok=False, reason="gui_gate",
                              error=refusal)
        return None

    def gui_click(self, params):
        refused = self._gate("gui_click", params)
        if refused is not None:
            return refused
        self.clicks += 1
        entry = self._slot_entry(int(params["slot"]))
        button = params.get("button", "left")
        shift = bool(params.get("shift", False))
        if entry[1] == "craft_out":
            self._take_output(shift)
            return self._done("gui_click", params)
        stack = self._content(entry)
        stack = list(stack) if stack else None
        if button == "left":
            if self.carried is None:
                self.carried, stack = stack, None
            elif stack is None:
                stack, self.carried = self.carried, None
            elif stack[0] == self.carried[0]:
                room = min(64 - stack[1], self.carried[1])
                stack[1] += room
                self.carried[1] -= room
                if self.carried[1] <= 0:
                    self.carried = None
            else:
                stack, self.carried = self.carried, stack
        else:
            if self.carried is None:
                if stack:
                    half = (stack[1] + 1) // 2
                    self.carried = [stack[0], half]
                    stack[1] -= half
            elif stack is None or stack[0] == self.carried[0]:
                stack = [self.carried[0], (stack[1] if stack else 0) + 1]
                self.carried[1] -= 1
                if self.carried[1] <= 0:
                    self.carried = None
        self._set(entry, stack)
        return self._done("gui_click", params)

    def _take_output(self, shift):
        made = self._match()
        if not made:
            return
        if shift:
            while made:
                self._add_to_inventory(*made)
                self._consume_grid()
                made = self._match()
            return
        if self.carried is None:
            self.carried = [made[0], made[1]]
        elif self.carried[0] == made[0]:
            self.carried[1] += made[1]
        else:
            return
        self._consume_grid()

    def gui_swap(self, params):
        refused = self._gate("gui_swap", params)
        if refused is not None:
            return refused
        self.swaps += 1
        entry = self._slot_entry(int(params["slot"]))
        hotbar = int(params["hotbar"]) - 1
        mine = self._content(entry)
        theirs = self.stacks.get(hotbar)
        self._set(entry, list(theirs) if theirs else None)
        if mine:
            self.stacks[hotbar] = list(mine)
        else:
            self.stacks.pop(hotbar, None)
        return self._done("gui_swap", params)


def zombie(at=6.0):
    return EntityRef(name="zombie", distance=at, hostile=True,
                     category="hostile", position=(at, 64.0, 0.0))


__all__ = ["GuiWorld", "zombie", "SCALE", "WINDOW"]
