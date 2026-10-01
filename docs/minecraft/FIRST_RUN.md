# First real run: checklist

The order matters: each part uses what the ones before it proved. Tick one
box per line. Stop at the first failure you cannot get past, and send back
what the "If it fails" block asks for.

Everything here has only ever been tested in unit tests and simulation —
see [NOT_TESTED_IN_GAME.md](NOT_TESTED_IN_GAME.md). The current mod jar has
never been loaded in a game.

**Where the results are.** Jarvis says a short summary out loud. The full
record is in the HUD log: every step of a task as
`[minecraft]   3. look 40ms -> success  | aim at oak_log (5, 64, 3)`, then
`[minecraft] task collect_logs: completed (12 steps)` and a line saying why
it ended. That last pair is "the task result text" below — copy it exactly,
along with the step lines just above it if the task failed.

**The emergency stop is F12.** On Windows, F12 releases every key and button
and ends the control session, even while Minecraft has focus
(`minecraft/emergency.py`: a global `GetAsyncKeyState` poll, 30 times a
second). Alt-Tab away from Minecraft does the same within one 40 ms tick.
Saying "stop" or "cancel" stops a running task and releases every key but
keeps the session open.

---

## a. Install and check

| # | Check | Pass | Fail |
|---|---|---|---|
| a1 | Quit Minecraft completely. Run `install_mod.bat`. It ends with `Installed: …markliv-bridge-1.0.0.jar` and `Done. Restart Minecraft, load a world…` (or `Already up to date`). | ☐ | ☐ |
| a2 | Run it once with Minecraft open: it refuses, saying `Minecraft is running (process …)` and to quit it first. | ☐ | ☐ |
| a3 | Run `doctor.bat`. It ends `[ ok ] Nothing wrong found. MARK LIV should start.` | ☐ | ☐ |
| a4 | Start Minecraft, load the test world (part b), then run `bridge_check.bat`. Under "IS THE MOD UP TO DATE?": head clearance `yes`, ground under trees `yes`, a mouse sensitivity number. Under "CAN IT NAVIGATE?": `YES — … columns of ground`. | ☐ | ☐ |

**If it fails, write down:** the step (a1–a4), and everything the window
printed, from the first line to the last.

## b. The test world

| # | Check | Pass | Fail |
|---|---|---|---|
| b1 | A new single-player world: **Survival**, difficulty **Peaceful**, **Allow Cheats: ON** (part h needs `/difficulty`, `/time` and `/summon`, which you type yourself — Jarvis cannot run commands). Somewhere with trees, open ground and exposed stone. | ☐ | ☐ |
| b2 | Start Jarvis. Say "start a Minecraft session". Approve the banner. Jarvis confirms; the banner said "This lasts until you say stop or press F12". | ☐ | ☐ |
| b3 | Ask "what's the Minecraft status?". It should say F12 will stop Minecraft control from anywhere (emergency scope `global`). If it says the stop is unavailable, F12 will not work — Alt-Tab still will. | ☐ | ☐ |
| b4 | Ask Jarvis to walk forward for two seconds, and press **F12** while it walks. It stops at once, and the next action asks for a new session. Start a new session. | ☐ | ☐ |

**If it fails, write down:** the step, Jarvis's exact words, and for b4
whether the character kept walking after F12, and for how long.

## c. Look around (read only)

Press F3 so the debug screen shows your coordinates.

| # | Check | Pass | Fail |
|---|---|---|---|
| c1 | Ask "what's around me?". It names the nearest log, stone and water with coordinates, your health and hunger, and whether it is night. | ☐ | ☐ |
| c2 | Its position matches F3's `XYZ` (to the block), and its facing matches F3's `Facing`. | ☐ | ☐ |
| c3 | Look at a block. Ask "what am I looking at?". The name and x, y, z match F3's `Targeted Block`. | ☐ | ☐ |
| c4 | Walk to the log it named. It is a log, at those coordinates. | ☐ | ☐ |

**If it fails, write down:** the step, Jarvis's exact words, and what F3
showed at that moment (a screenshot with F3 open is ideal).

## d. Walking

| # | Check | Pass | Fail |
|---|---|---|---|
| d1 | "Walk forward for two seconds." It walks, then stops. | ☐ | ☐ |
| d2 | Read your X and Z from F3. Ask it to walk to a spot 10 blocks away over open ground ("walk to x … z …"). It arrives within 1.5 blocks (what it counts as arrived), and says where it ended up. | ☐ | ☐ |
| d3 | Put a wall two blocks high between you and a spot 6–8 blocks away (place the blocks yourself). Ask it to walk there. It goes round the wall, not into it. | ☐ | ☐ |
| d4 | While it walks, say "stop". It stops at once and says so. | ☐ | ☐ |

**If it fails, write down:** the step, Jarvis's exact words, the task result
text (with the step lines), and your F3 position before and after.

## e. Gathering

Put an axe in hotbar slot 2 and a pickaxe in slot 3, and hold something
else in slot 1.

| # | Check | Pass | Fail |
|---|---|---|---|
| e1 | "Collect 3 logs." It walks to one tree, breaks logs, picks them up, and the report names the tree. | ☐ | ☐ |
| e2 | During e1 it took up the **axe** before the first swing, and slot 1 was selected again afterwards. | ☐ | ☐ |
| e3 | The 3 logs are in your inventory, and the report says "collected" (not just "broke"). | ☐ | ☐ |
| e4 | "Collect 3 stone." It walks to exposed stone, takes up the **pickaxe**, and you end with 3 cobblestone. Slot 1 again afterwards. | ☐ | ☐ |
| e5 | It never broke the block it was standing on, and never dug down. | ☐ | ☐ |

**If it fails, write down:** the step, Jarvis's exact words, the task result
text with all its step lines, and which hotbar slot was selected during the
swings.

## f. Inventory and crafting

| # | Check | Pass | Fail |
|---|---|---|---|
| f1 | "Open the inventory." It opens. "Close the inventory." It closes. | ☐ | ☐ |
| f2 | With nothing open, "close the inventory" again: it refuses, saying no screen is open, and the pause menu does **not** appear. | ☐ | ☐ |
| f3 | "Craft oak planks" (or the planks for the logs you have). Planks appear; the logs went down. | ☐ | ☐ |
| f4 | "Craft sticks." It needs planks only. (A pickaxe needs sticks; it does not craft them for you — it says what is missing.) | ☐ | ☐ |
| f5 | "Craft a crafting table." One appears in the inventory. | ☐ | ☐ |
| f6 | "Craft a wooden pickaxe." With the table in your inventory and none placed, it puts the table down beside you first, opens it, and crafts. The pickaxe is in the inventory. | ☐ | ☐ |
| f7 | No item ended up dropped on the ground, and every screen it opened is closed again. | ☐ | ☐ |

**If it fails, write down:** the step, Jarvis's exact words, the task result
text with its step lines, and a screenshot of the screen if one was left
open.

## g. Placing and building

Have at least 40 cobblestone or dirt in the hotbar. Use flat open ground.

| # | Check | Pass | Fail |
|---|---|---|---|
| g1 | Look at a block on the ground and read F3's `Targeted Block`. Ask it to place one cobblestone on top of it (x, same y + 1, z). The block appears there and the stack went down by one. | ☐ | ☐ |
| g2 | "Build a line of 5 cobblestone going east from x y z." Five blocks, in a line, where asked. The report lists each cell placed and any that failed. | ☐ | ☐ |
| g3 | "Build a shelter out of cobblestone." Before starting it tells you the plan: a hollow 3×3×3 with a doorway, a roof and a three-block step — 26 blocks. | ☐ | ☐ |
| g4 | It builds part of it and says to ask again; say "carry on" until it is finished (three or four asks). Walls two high with a doorway, a roof, the step along one side. | ☐ | ☐ |

**If it fails, write down:** the step, Jarvis's exact words, the task result
text with its step lines, and a screenshot of what was built.

## h. Mobs (last)

Hostile mobs need a difficulty above Peaceful. Have a sword in the hotbar,
full health, and F12 ready.

| # | Check | Pass | Fail |
|---|---|---|---|
| h1 | Type `/difficulty easy`, then `/time set night` (zombies burn in daylight), then `/summon minecraft:zombie ~6 ~ ~`. | ☐ | ☐ |
| h2 | Ask it to collect a log while the zombie is within 5 blocks: it refuses, or stops, and says a hostile mob is close. | ☐ | ☐ |
| h3 | "Run away." It runs (sprinting on clear ground) until the zombie is over 12 blocks away, and says where the zombie ended up. | ☐ | ☐ |
| h4 | Summon another zombie close by. "Fight the zombie." It takes up the sword, walks into reach, and hits only the zombie. Afterwards your old hotbar slot is selected again. | ☐ | ☐ |
| h5 | It says "most likely killed" or "gone", not "killed" — the game does not report a mob's health. | ☐ | ☐ |
| h6 | Ask it to fight with your health below 12 (take some fall damage first): it refuses to start. | ☐ | ☐ |
| h7 | Type `/difficulty peaceful` to finish. | ☐ | ☐ |

**If it fails, write down:** the step, Jarvis's exact words, the task result
text with its step lines, your health before and after, and whether
anything other than the zombie was hit.
