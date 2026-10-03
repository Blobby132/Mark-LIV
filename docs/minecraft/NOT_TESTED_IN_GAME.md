# Not tested in the game

Everything below has been verified only by unit tests, the Java test
harness, or a simulated world (`tests/support/`). None of it has run inside
Minecraft. [FIRST_RUN.md](FIRST_RUN.md) is the checklist that tries it.

**What has run in the game.** The live logs from the first three runs
(up to 2026-09-24) exercised starting a session, `look`, `move`, `mine`,
`navigate_to`, `collect_logs` and `break_block` — the third run with the jar
that reads the ground nearest the feet (commits 56081e0, 6ddeaa2). Session
start reported the F12 hook as `global`; no log records F12 being pressed.
Everything committed from 2b5c11c ("Third run: …") onward is untested in the
game, and so is every jar built since: **the current
`mods/markliv-bridge-1.0.0.jar` has never been loaded in Minecraft.**

The "From" column is the report that listed it as unverified (or the commit, where it came after the last run without a report saying so): **R-A** the
A/N/C round, **R-D** the danger-stop round, **R-B** the B round (screens,
building, survival), **R-1** the items 1–7 round, **R-F** the feature-list
round, **R-O** the ore-finding and digging round, **R-H** the dig hardening
round.

## The bridge mod (Java)

| Capability | Source | Only verified by | From |
|---|---|---|---|
| The open screen's kind, its slots and their window-pixel positions, the pointer, the stack on the pointer, the game mode | `fabric-mod/src/main/java/com/markliv/bridge/Gui.java` | `tests/bridge/test_bridge_gui_fields.py` (Java harness `GuiCheck.java`) | R-B |
| The access widener that lets the mod read where a container screen is drawn (`leftPos`, `topPos`) | `fabric-mod/src/main/resources/markliv-bridge.accesswidener` | `tests/bridge/test_bridge_gui_fields.py` checks it is declared; never loaded | R-B |
| `near_blocks`: every non-air block within 4 of the player, and how far out the list is complete; its size (about 4.8 KB, under 12 KB at its cap) | `fabric-mod/src/main/java/com/markliv/bridge/NearBlocks.java` | `tests/bridge/test_bridge_near_blocks.py` (`NearBlocksCheck.java`) | R-B |
| The target's and each mob's `category` (hostile, passive, player, …) and position | `MarkLivBridge.java` (`categoryOf`), `Kinds.java` | `tests/minecraft/test_minecraft_attack_precondition.py`, `test_minecraft_item_drops.py` | R-B, R-1 |
| What a dropped item is (`item` on its entity entry) | `Kinds.java`, `MarkLivBridge.java` | `tests/minecraft/test_minecraft_item_drops.py`, `tests/bridge/test_bridge_nearest.py` | R-A (N7) |
| Logs reported up to 12 blocks above the feet, following the trunk | `ColumnScan.java` (`TREE_UP`), `MarkLivBridge.java` | `tests/bridge/test_bridge_floor_scan.py` (`ColumnScanCheck.java`) | R-A (N8) |
| The nearest notable blocks of each kind, not the first 64 found | `Nearest.java` | `tests/bridge/test_bridge_nearest.py` (`NearestCheck.java`) | committed after the last run (0060ef0) |
| The one list of logs and hazards (crimson and warped stems, dripstone, portals, sculk, tripwire) | `Kinds.java`, `minecraft/navigation.py` | `tests/minecraft/test_minecraft_block_names.py` | R-A (C10) |
| The terrain scan's cost (collision computed once per block, names cached) — never timed | `ColumnScan.java` | `tests/bridge/test_bridge_scan_cost.py` | R-A (C11) |
| `ores`: the nearest ore within 24 sideways, 32 down, 16 up, buried ones included; `exposed` and `fluid_near`; 16 of a kind, 64 in all, 16 KB; sections whose palette has no ore skipped; at most one pass a second, spread over the 5 Hz snapshots. Cost measured only in the harness (0.3 ms a step on average with array reads standing in for the game's) | `OreScan.java`, `MarkLivBridge.java` (`LevelOres`) | `tests/bridge/test_bridge_ore_scan.py` (`OreScanCheck.java`) | R-O |
| `singleplayer` (`Minecraft.hasSingleplayerServer`); the ore scan does not run when it is false | `MarkLivBridge.java` | nothing: needs a real game | R-O |
| `near_blocks` from five below the feet to three above, with a `grid` of every cell (palette and runs), fluids named, `complete` when no cell is in an unloaded chunk | `NearBlocks.java`, `MarkLivBridge.java` | `tests/bridge/test_bridge_near_blocks.py` | R-O |
| `features`: the list of what the jar reports, sent in a menu too; a jar without it, or missing a name Jarvis needs, counted as outdated | `MarkLivBridge.java` (`FEATURES`), `minecraft/mod_bridge.py` | `tests/bridge/test_bridge_features.py` | R-F |

## Screens and crafting

| Capability | Source | Only verified by | From |
|---|---|---|---|
| The click gate: allowed screens and modes, the pointer inside the slot's shrunken rect, no hostile near, no health lost | `minecraft/gui.py`, `minecraft/controller.py` (`gui_click`, `gui_swap`) | `tests/minecraft/test_minecraft_gui.py`, `test_minecraft_gui_actions.py` | R-B |
| Bringing the pointer onto a slot: pointer acceleration, learnt from the pointer the game reports | `minecraft/skills/base.py` (`_learn_pointer`), `minecraft/controller.py` (`gui_point`) | `tests/minecraft/test_minecraft_craft_item.py`, `test_minecraft_hotbar_fetch.py` (simulated `pointer_gain` in `tests/support/gui_world.py`) | R-B |
| Crafting by clicks: pick up a stack, one per cell, put the rest back, shift-click the output; the 2×2 and the 3×3; putting a table down first | `minecraft/skills/craft.py`, `minecraft/recipes.py` | `tests/minecraft/test_minecraft_craft_item.py` | R-B |
| Moving an item from the main inventory to the hotbar (one number-key swap) | `minecraft/skills/hotbar.py` | `tests/minecraft/test_minecraft_hotbar_fetch.py` | R-B |
| `craft_item` taking up a safe slot before opening a table, and putting the old slot back | `minecraft/skills/craft.py` (`_safe_hand`, `_restore_hand`) | `tests/minecraft/test_minecraft_interact_held_item.py` | R-1 |
| The runner pressing ESC to close a screen its task opened, after a cancel, a timeout or an error | `minecraft/task_runner.py` (`_track_screen`, `_close_own_screen`) | `tests/minecraft/test_minecraft_screen_guard.py` | R-1 |
| No gameplay input while a screen is open | `minecraft/controller.py` (`SCREEN_BLOCKED_ACTIONS`) | `tests/minecraft/test_minecraft_screen_guard.py` | R-1 |
| `inventory close` refused when the bridge reports no screen | `minecraft/controller.py` (`_close_refusal`) | `tests/minecraft/test_minecraft_inventory_close.py` | R-1 |
| After a key that opens or closes a screen (inventory, interact with a table or chest), waiting up to 1 s for a reading taken after it and saying "opened", "closed" or "not confirmed"; every screen-dependent refusal judged on a reading newer than the last such key | `minecraft/controller.py` (`_screen_reading`, `_confirm_screen`) | `tests/minecraft/test_minecraft_screen_freshness.py` (a fake bridge 200 ms behind the game) | R-O |
| An older jar told apart from "no screen open": `inventory close` and `craft_item` say the mod is older and name the field (`game_mode`) | `minecraft/controller.py` (`_close_refusal`), `minecraft/skills/craft.py` (`_no_game_mode`) | `tests/minecraft/test_minecraft_screen_messages.py` | R-F |
| The one-line "older than this Jarvis" notice, once, at session start or the first task that needs a missing feature; HUD and reply | `actions/minecraft.py` (`_feature_notice`), `minecraft/mod_bridge.py` (`outdated_notice`) | `tests/minecraft/test_minecraft_feature_notice.py` | R-F |
| `find_ores`: the scan's ore nearest first with depth, exposed or buried, water or lava near; refused unless the reading says single-player; never more than the scan listed | `minecraft/ores.py`, `actions/minecraft.py` (`_find_ores`) | `tests/minecraft/test_minecraft_find_ores.py` | R-O |
| `bridge_check`'s table of the features in the live payload | `tools/bridge_check.py` (`feature_rows`) | `tests/bridge/test_bridge_check_features.py` | R-F |

## Placing and building

| Capability | Source | Only verified by | From |
|---|---|---|---|
| Face aiming: pressing only when the crosshair is on the right block *and* face; proving the result twice | `minecraft/skills/place_build.py` (`PlaceBlockAt`), `minecraft/building.py`, `minecraft/aiming.py` | `tests/minecraft/test_minecraft_place_block_at.py`, `test_minecraft_place_precondition.py` | R-B |
| `build_line` | `minecraft/skills/place_build.py` (`BuildLine`) | `tests/minecraft/test_minecraft_build_line.py` | R-B |
| `build_blueprint` and the shelter numbers: 14 wall blocks with a doorway, a 3-block step, a 9-block roof (26); about fifteen blocks a task; carrying on | `minecraft/building.py`, `minecraft/skills/place_build.py` (`BuildBlueprint`) | `tests/minecraft/test_minecraft_build_blueprint.py` | R-B |
| The hop onto a single raised block, and giving up after 3 hops at the same block (the overshoot came from the simulator's walk speed) | `minecraft/skills/navigate.py` (`MAX_HOPS_ONTO`) | `tests/minecraft/test_minecraft_navigate_fixes.py` | R-B |
| `place` and `interact` refusing buckets, flint and steel, TNT, spawn eggs and the like; `use_item` pouring or lighting only when the item is named | `minecraft/action_spec.py` (`interact_refusal`, `use_item_refusal`), `minecraft/controller.py` | `tests/minecraft/test_minecraft_place_held_item.py`, `test_minecraft_interact_held_item.py` | R-1 |
| Jump, then place (pillar up, bridge): **not built** — a design only, with a timing window (about 0.27 s) from game constants, never measured | [jump-place.md](jump-place.md) | nothing — there is no code | R-B |

## Mobs and danger

| Capability | Source | Only verified by | From |
|---|---|---|---|
| `flee` against real mobs: running to the furthest reachable ground, sprinting on clear ground, until the nearest hostile is over 12 blocks away | `minecraft/skills/combat.py` (`Flee`) | `tests/minecraft/test_minecraft_flee.py` (simulated `MobWorld`) | R-B |
| `fight` against real mobs: walk into reach, aim at the body, tap every other step; a hit judged by knockback (the check wants 0.2 blocks; about 0.4 in the game, 0.5 in the simulator); "gone" as "most likely killed" | `minecraft/skills/combat.py` (`Fight`), `minecraft/verification.py` (`knocked_back`) | `tests/minecraft/test_minecraft_fight.py` (simulated arena) | R-B |
| `fight`: the mobs it will not walk up to (`NEVER_MELEE`, matched by the names the game reports), the start-health floor (12), taking up the best sword or axe and putting the slot back | `minecraft/skills/combat.py` | `tests/minecraft/test_minecraft_fight.py` | R-1 |
| Every `attack` hits only a mob the game calls hostile | `minecraft/controller.py` (`_entity_refusal`), `actions/minecraft.py` (`_entity_probe`) | `tests/minecraft/test_minecraft_attack_default.py`, `test_minecraft_attack_precondition.py` | R-1 |
| The danger watch between steps: a lost heart, or a hostile within 5 blocks at about your level, stops or refuses a task; `navigate_to` keeps walking | `minecraft/danger.py`, `minecraft/task_runner.py` | `tests/minecraft/test_minecraft_danger.py`, `test_minecraft_tasks.py` | R-D |
| The hazard probe inside a hold: mining, eating, placing or interacting lets go for a hostile within 3 blocks or 2 health lost | `actions/minecraft.py` (`_hazard_probe`), `minecraft/controller.py` | `tests/minecraft/test_minecraft_hazard_probe.py` | R-A (A2, A4) |
| The dusk thresholds (12000, 13000, 23000 ticks; a hostile within 24 blocks), from game constants, never measured; overworld only | `minecraft/danger.py` (`DUSK_START`, `NIGHT_START`, `NIGHT_END`, `has_night`) | `tests/minecraft/test_minecraft_night.py` | R-B, R-1 |

## Walking, gathering, eating

| Capability | Source | Only verified by | From |
|---|---|---|---|
| Going round a mob in a gap; diagonal steps not cutting corners; a failed hop counted as a stall; detour tries refilling; not skipping a new route's first waypoints | `minecraft/skills/navigate.py`, `minecraft/navigation.py`, `minecraft/stuck.py` | `tests/minecraft/test_minecraft_navigation.py`, `test_minecraft_navigate_fixes.py` | R-A (N1–N5) |
| Step pacing: counting the wait for a fresh reading toward the pause (about 400 ms a step, measured in a timed simulation) | `minecraft/task_runner.py` | `tests/minecraft/test_minecraft_step_pacing.py` | R-A (N6) |
| Re-planning when the target log changes mid-walk; picking up what was broken before giving up; trying every drop | `minecraft/skills/collect.py` | `tests/minecraft/test_collect_logs_walker.py`, `test_collect_logs_trees.py`, `test_live_run_regressions.py` | R-A (N9), R-D |
| `dig_to`: a staircase under the hard rules (R1-R8 of `minecraft/digging.py`), each re-checked before every swing; the pickaxe chosen and refused when it cannot harvest; the cell air in the next reading, the feet in the next cell; a cave reported, not entered; asking again carries on. The voxel world's gravity, falling gravel and flowing water are the simulation's, not the game's | `minecraft/skills/dig.py`, `minecraft/digging.py` | `tests/minecraft/test_minecraft_dig_to.py` (DigWorld), `tests/minecraft/test_minecraft_digging.py` | R-O |
| R4's hazards: no step onto or into a block in the shared `HAZARDS` (magma, campfires, powder snow, cobweb, fire, berry bush, wither rose, cactus ...), none with a cactus beside the body, judged by the block's name whatever the mod says of its solidity; the pickup walk too. Whether the game's names for these match the list was checked only against the names the mod sends | `minecraft/blocks.py`, `minecraft/digging.py` (`_r4_hazards`) | `tests/minecraft/test_minecraft_digging.py` (`FloorHazardTests`), `tests/minecraft/test_minecraft_dig_worlds.py` (magma floor) | R-H |
| R9: no planned cell within five (on every axis) of sculk, a sculk sensor, shrieker or catalyst, reinforced deepslate or a spawner; R8 stops the dig when one comes within five of the body. Only the grid's box is seen -- four to the side, three up -- so one five or six off to the side is caught by R8 as it comes into view, not by R9 beforehand. Whether real ancient cities and monster rooms are seen early enough this way is untested | `minecraft/blocks.py` (`WARDEN_OR_SPAWNER`), `minecraft/digging.py` (`_r9_warden`, `_r8_warden`) | `tests/minecraft/test_minecraft_digging.py` (`R9WardenTests`, `R8WardenTests`), `tests/minecraft/test_minecraft_dig_worlds.py` (monster room, sculk coming into view) | R-H |
| `mine_ore`: the nearest listed ore with no water or lava beside it, within the dig limits; a staircase to beside it; the vein it can reach and see, each block under `check_break`; stepping into the hole for the rest and for the drops; the count from the inventory; on a refused route, the nearest other ore offered; the plan said before it starts. DigWorld's ore scan, line of sight and pickup range (1.4 blocks along x and z from the cell's middle) are the simulation's, not the game's | `minecraft/skills/dig.py` (`MineOre`), `minecraft/ores.py` (`choose`), `actions/minecraft.py` (`_mine_ore_plan`) | `tests/minecraft/test_minecraft_mine_ore.py` (DigWorld) | R-O |
| Every hard rule end to end -- skill, runner and simulated world together -- in a named world for each danger: a mineshaft (R1), a water pocket (R2), a gravel ceiling (R3), a hidden shaft under the first stair (R4), a long tunnel (R5), a cave opening (R6), a lava pocket (R7), a buried vein and a floating ore; each R1-R7 world run again with that rule removed to show the danger is real. R8 from worlds that change mid-dig: health lost, water appearing, a cave-in, a hostile mob, a fall, a pickaxe that cannot harvest, the session ending. In these worlds R2 is reached only on the first reading: after a step, R8's watch for fluid within two blocks of the body is wider, so it stops first. R7 looks for lava only inside the grid's box (three above the feet, four sideways): a hole in the box refuses, but lava further out is not looked for -- it could only reach the dig through a cell the grid shows | `minecraft/skills/dig.py`, `minecraft/digging.py`, `minecraft/task_runner.py` | `tests/minecraft/test_minecraft_dig_worlds.py` (`tests/support/dig_world.py`) | R-O |
| `fell_tree` not calling a tree gone when it reached the highest row the scan reports logs in (`log_ceiling`) | `minecraft/skills/collect_logs.py` (`_note_ceiling`), `minecraft/mod_bridge.py` (`_log_ceiling`) | `tests/minecraft/test_fell_tree_scan_ceiling.py` (simulated tree, read through old- and new-jar payloads) | R-F |
| Taking up the best hotbar tool before each swing, and putting the slot back | `minecraft/skills/collect.py` (`_HoldsTheRightTool`), `minecraft/mining.py` | `tests/minecraft/test_minecraft_tools.py` | R-B (B1) |
| `collect_blocks` (stone, dirt, sand, gravel, ores): only exposed blocks, never the one underfoot, none beside water or lava | `minecraft/skills/collect_blocks.py` | `tests/minecraft/test_collect_blocks.py` | R-B (B2) |
| `eat_food`: choosing food, eating for each food's own time (capped at 3 s), looking up first if a chest or door is under the crosshair | `minecraft/skills/eat.py` | `tests/minecraft/test_minecraft_eating.py` | R-D, R-A (C8) |
| `look_around` saying how many trees, your health, hunger, the food on your hotbar and whether it is night | `actions/minecraft.py` (`_around_line`, `_player_bits`) | `tests/minecraft/test_minecraft_navigation.py`, `test_minecraft_night.py` | R-D |
| A brief "cannot see the window" blip not ending the session | `minecraft/controller.py` (`WINDOW_MISS_SPAN_S`, `WINDOW_MISS_RUN_GAP_S`) | `tests/minecraft/test_minecraft_safety.py` | R-A (C9) |
| `install_mod.bat` refusing while Minecraft runs, and checking the copy by content | `tools/install_mod.py` | `tests/bridge/test_install_mod.py` | R-D |

## Not Minecraft, and not testable here

Anything needing Windows (the `.bat` files, the global F12 hook, SendInput),
the Qt HUD, audio devices or the live Gemini session has not been run where
these changes were made. Those are covered by the first-run checklist only
where they touch Minecraft.
