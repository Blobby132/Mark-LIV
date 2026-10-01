# Clicking inside Minecraft's inventory screens

Status: design, written before the code. It describes what the bridge mod
reports, which screens may be clicked, the gate every click passes, when a
GUI task gives up, and what the confirmation banner says about it.

## Why this is its own layer

Until now nothing clicked inside a screen. `inventory` opened and closed the
player inventory with a key, and everything else was gameplay: keys and
mouse buttons aimed at the world through the crosshair. Crafting needs the
mouse *pointer*: move it onto one slot, click, move to the next. A click in
the wrong place is not a harmless miss — clicking outside every slot drops
the carried stack on the ground, a shift-click moves a whole stack, and in
the creative inventory a click can delete items. So a click is never sent on
a computed position alone. It is sent only when the game itself reports that
the cursor is over the slot meant.

No new input primitive. A pointer move is the same relative
`move_mouse_relative` the camera uses (outside a screen it turns the view;
inside one it moves the pointer), and a click is a tap of the two mouse
buttons that already exist. `shift` is already an allowed key (it is sneak),
so shift-click needs no new key either.

## What the mod reports (additive, schema /4)

Only while a screen is open. Older readers ignore these keys; an older jar
does not send them, and the GUI layer then refuses to click at all.

| key | when | value |
|---|---|---|
| `screen` | any screen open | `{"kind": "inventory" \| "crafting_table" \| "furnace" \| "chest" \| "pause" \| "other"}` |
| `gui` | a container screen | `{"scale": s, "window_px": [w, h], "cursor_px": [x, y]}` — `scale` is window pixels per GUI unit |
| `slots` | a container screen | `[{"i", "role", "x", "y", "item", "count"}]` |
| `carried` | a container screen | `{"name", "count"}` or `null` — the stack on the pointer |
| `game_mode` | always | `"survival"`, `"creative"`, `"adventure"` or `"spectator"` |

`slots[].i` is the menu slot index (what a click addresses); `x`, `y` are the
slot's **centre in window pixels**, in the same coordinates as `cursor_px`;
`role` is one of `craft_in`, `craft_out`, `inventory`, `hotbar`, `armor`,
`offhand`, `other`; `item` is the registry name or `null`. A slot is 16×16
GUI units, so its rect is centre ± 8 × `scale`.

Slot positions come from `AbstractContainerScreen.leftPos` / `topPos`, which
are protected; an access widener (`markliv-bridge.accesswidener`) makes them
readable. No Fabric API. The mod still only reads: it writes one file and
accepts no commands.

The slot list is capped (`MAX_SLOTS`, 64 entries), and the payload size is
measured by the tests.

## Which screens may be clicked

Allow-listed: **`inventory`** (the survival player inventory, with its 2×2
crafting grid) and **`crafting_table`** (3×3). Never: `pause`, `other`,
`chest`, `furnace`, the creative inventory, or any screen while the game mode
is `creative` or `spectator`. A chest or furnace screen is reported (so the
assistant can say what is open) but not clicked.

## The click gate

The controller checks all of these **from a fresh bridge read, immediately
before pressing**, and presses nothing unless every one holds. The rules are
pure functions in `minecraft/gui.py`; the controller asks them through an
injected `gui_probe`, like `hazard_probe` and `held_item_probe`.

1. The screen kind is allow-listed and the game mode is survival or adventure.
2. The click names a slot `i` that the game reports in this screen.
3. The reported cursor is inside that slot's rect, shrunk by one GUI unit on
   each side — never on a border, never between slots. This is also what
   makes "never click outside every slot" impossible to break.
4. The cursor is inside the game window with a margin (`WINDOW_MARGIN_PX`).
5. Minecraft has focus (the controller's ordinary guard).
6. No hostile mob within `GUI_HOSTILE_RADIUS` (8 blocks), and health has not
   dropped since the screen was opened.
7. The reading is fresh: no older than `GUI_MAX_AGE_S`.

Pointer moves (`gui_point`) need only 1, 4 (after the move cannot be known,
so the move is bounded instead: at most `MAX_GUI_STEP_PX` per call) and 5.

A refused click returns `stopped_reason: "gui_gate"` and the rule that
failed.

## Moving the pointer

Closed loop on the cursor the mod reports: move by a relative delta,
observe, correct. The OS applies pointer acceleration inside screens (the
camera bypasses it with raw input), so the pixels-per-pixel gain is measured
from each move and smoothed, not assumed. At most `MAX_CORRECTIONS` (8)
corrections per target; if the cursor still is not inside the slot, the task
closes the screen and says so.

## Fetching into the hotbar (B3g)

`eat_food` and the mining tasks use the same layer for one thing more: a
stack they need that is only in the main inventory. `HotbarFetch` opens the
inventory, brings the pointer onto the stack (closed loop, as above), sends
`gui_swap` — the hotbar number key, which swaps the slot under the pointer
with that hotbar slot — and closes. Nothing is ever on the pointer, so
nothing can drop. The swap passes the click gate like a click. Its result
is checked against the reported slots, and then against the inventory once
the screen is shut.

The hotbar slot it fills is the first empty one, else the last one holding
something not worth keeping at hand (a block, not a tool, weapon, food or
bucket), and never the selected one while another will do. What was there
goes where the fetched stack came from. A mining task fetches a tool when
nothing in the hotbar harvests the block, or when the stored tool saves
`FETCH_WORTH_S` (1.5 s) a block. It makes the trip at most once per task.

While the screen is open the fetch keeps the rules below. The runner's own
danger watch stands down for that time, because it would stop the task with
the screen left open. The runner reads each task's watch flags at every
step, not just once. Starving costs a point of health every four seconds on
its own, so for `eat_food` a trip stopped by health alone, with no hostile
near, is made once more.

## Abort conditions

A GUI task closes the screen (the `inventory` action, `state=close`) and
reports, rather than continue, when:

- the pointer will not converge on a slot within 8 corrections;
- the click gate refuses, for any rule;
- a hostile comes within 8 blocks or health drops, checked between clicks;
- the screen kind changes or the screen closes under it;
- a click's result is not what was expected (the stack did not land, the
  output did not appear), checked against the slots the mod reports;
- the per-task click cap (`MAX_GUI_CLICKS`, 64) is reached.

Whatever is on the pointer when it stops is put back into a free inventory
slot first, if the gate allows; otherwise it says that a stack is still on
the pointer.

## What the confirmation covers

`GRANT_SUMMARY` gains, in the user's words: "move items inside the inventory
and crafting-table screens — one click at a time, each checked against the
game's own report of where the pointer is". The banner shows it; the README's
"what it deliberately cannot do" no longer says it cannot click in the
inventory, and says instead which screens it never clicks.
`NOT_YET_POSSIBLE["craft_item"]` is replaced by the real task.

## What this does not do

- Click in a chest, furnace, anvil, villager or creative screen.
- Drag (left-drag spreads a stack); each click is a single tap.
- Drop anything: no click outside a slot, and `Q` is not used in screens.
- Act without the bridge mod.
