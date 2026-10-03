# Architecture: the Minecraft subsystem

How a spoken request becomes keys pressed in Minecraft, which module does
what, and the layers that stop it doing anything else. The module docstrings
hold the reasoning; this page is the map.

## One request, end to end

```
voice / model
  └─ actions/minecraft.py          the adapter: which capability a call needs,
       │                           the session banner, ActionResult → words
       │                           (its long tool text: actions/_minecraft_text.py)
       ├─ core/permissions guard   the broker, deciding from core/capabilities.py
       ├─ minecraft/capabilities   phase gate: can only subtract from that table
       └─ minecraft/controller     session check, focus guard, preconditions,
            │                      ledger, deadman, hazard probe, click gate
            └─ minecraft/input_backend   SendInput, 21 keys and 2 buttons only

run_task
  └─ minecraft/task_slot           one task at a time, off the voice thread
       └─ minecraft/task_runner    observe → act → observe → verify, bounded;
            │                      the danger watch between steps
            └─ minecraft/skills/   plan the next Step from the state; never
                                   touch the controller, ledger or backend

reading the game
  fabric-mod (read-only)  →  JSON snapshot file, 5×/s  →  minecraft/mod_bridge
                                                       →  minecraft/state.WorldState
  (fallback: the F3 overlay read by OCR injected from core/ocr.py)
```

The model chooses *which* action or skill to run and with what parameters.
It never chooses what a skill does step by step, and nothing it says can
widen what the session allows.

## The adapter, `actions/`

| Module | Job |
|---|---|
| `minecraft.py` | The one action the loader discovers: TOOL, the handler, capability resolution, the session banner, the injected probes. |
| `_minecraft_text.py` | TOOL's long text: the description and every parameter's. Data only; skipped by discovery (leading `_`). |

## Modules in `minecraft/`

**Input and stopping**

| Module | Job |
|---|---|
| `controller.py` | Every action: authorise, parse, check preconditions, hold, release. Every way a hold is stopped. |
| `action_spec.py` | The vocabulary and its limits: durations, keys, which params are refused, held-item rules. Pure. |
| `input_backend.py` | The only keys and buttons that can be pressed; `FakeInputBackend` for tests. |
| `ledger.py` | What is held right now; `release_all()`. |
| `emergency.py` | F12, on its own thread. |
| `window.py`, `process.py` | Which window and process are Minecraft, and whether it has focus. |
| `session.py` | The consent boundary and `GRANT_SUMMARY`. |
| `capabilities.py` | The phase gate: which declared capabilities this build attempts. |
| `errors.py` | Every refusal and failure, named. |

**Reading the game**

| Module | Job |
|---|---|
| `mod_bridge.py` | Reads the mod's snapshot; refuses stale data and unknown schema versions. |
| `state.py` | `WorldState`, with per-field provenance (`exact`, `inferred`, `unknown`). |
| `debug_overlay.py`, `perception.py`, `observation.py` | The pre-mod route: F3 text and screen capture of the game window only. |

**Rules (pure: they read a state and return an answer, pressing nothing)**

| Module | Job |
|---|---|
| `navigation.py` | The local terrain map and the pathfinder. Imports only heapq, math, dataclasses and `aiming` (which imports only math and dataclasses). |
| `aiming.py` | "Look at that" → mouse pixels, with calibration. |
| `mining.py` | Break times, the right tool, whether a block drops. |
| `building.py` | Which cell may take a block, and the face to click. |
| `gui.py` | When a click inside a screen may happen (the click gate). Imports only heapq, math, dataclasses. |
| `recipes.py` | The recipes `craft_item` knows. Data. |
| `ores.py` | What the mod's ore scan lists, filtered and put in words for `find_ores`; refuses outside a single-player world. Imports only math and dataclasses. |
| `danger.py` | Should a task stop for health or a mob; the dusk note. |
| `verification.py` | Did that step actually work? |
| `stuck.py`, `progress.py` | Why a walk stopped; noticing nothing is happening. |

**Tasks**

| Module | Job |
|---|---|
| `task_slot.py` | One task at a time, in the background; cancel. |
| `task_runner.py` | The bounded loop: at most 45 steps and 120 seconds, a verified result per step, the danger watch, and closing a screen the task opened if it ends early. |
| `skills/` | What a task does: below. |

## How the modules layer

The import graph of `minecraft/` has no cycle (module-level imports, and
lazy ones inside functions too). Each module imports only from layers below
its own; a layer is the longest chain of imports beneath it:

| Layer | Modules |
|---|---|
| 0 | `aiming` `building` `capabilities` `danger` `emergency` `errors` `gui` `ledger` `mining` `ores` `process` `recipes` `session` `state` |
| 1 | `action_spec` `debug_overlay` `input_backend` `mod_bridge` `navigation` `perception` `task_slot` `verification` `window` |
| 2 | `controller` `observation` `progress` `stuck` `skills.base` |
| 3 | `task_runner` |
| 4 | `skills.hotbar` `skills.navigate` |
| 5 | `skills.collect` `skills.combat` `skills.eat` `skills.place_build` |
| 6 | `skills.collect_blocks` `skills.collect_logs` `skills.craft` |
| 7 | `skills.aim` |
| 8 | `skills` (the registry) |

The 29 top-level modules stay in one folder, on purpose. Grouped by role
-- control, perception, rules, tasks, as the tables above do -- the groups
would import each other in a circle: `controller` uses `gui` (the click
gate, a rule), `verification` (a rule) reads `state` (perception), and
`observation` (perception) uses `window` (control). Subpackages would
either carry that cycle or split modules that belong together, so the
layering is written down here instead, and a new module goes in the layer
its imports put it in.

## The skills package, `minecraft/skills/`

| Module | Skills | Notes |
|---|---|---|
| `base.py` | — | The `Skill` protocol; shared constants and crosshair helpers; the food tables; the check for blocks a right-click would use instead; inventory-screen helpers. |
| `hotbar.py` | — | `HotbarFetch`: an item from the main inventory into the hotbar through the inventory screen. Used by collect, eat and place_build. |
| `navigate.py` | `walk_forward`, `survey`, `find_block`, `navigate_to` | |
| `collect.py` | `break_block` | What the gathering skills share: `_HoldsTheRightTool` (the best tool before each swing, the slot put back after), `_Gatherer` (walk, aim, break, pick up the drop), and the tree and drop helpers. |
| `collect_logs.py` | `collect_logs`, `fell_tree` | On `_Gatherer`. |
| `collect_blocks.py` | `collect_blocks` | On `_Gatherer`; only exposed blocks it is safe to break. |
| `aim.py` | `aim_at_block`, `mine_block` | Uses `collect_logs` for the mining hold. |
| `eat.py` | `eat_food` | |
| `craft.py` | `craft_item` | |
| `place_build.py` | `place_block`, `place_block_at`, `build_line`, `build_blueprint` | The blueprint memory that lets "carry on" resume. |
| `combat.py` | `flee`, `fight` | `NEVER_MELEE`, the start-health floor, the weapon choice. |
| `__init__.py` | — | The registry (`BUILTIN_SKILLS`, `create`, `available`), `NOT_YET_POSSIBLE`, and the public names in `__all__`. Anything else is imported from its submodule. |

Each module imports only from those above it in the table (craft also from
place_build), so there is no import cycle. The registry is a fixed table in
source: nothing can add a skill at runtime.

A skill is `plan(state, step_index, history) -> Step | None`. It describes
an action and the expected result; the runner decides whether to run it, and
the controller whether to press anything.

## The safety layers

Each layer refuses on its own; none trusts another to have caught something.
Each refusal says why, and nothing is pressed.

### 1. Permission gate

- **Where:** `core/capabilities.py` (the table), the broker in
  `core/permissions`, `actions/minecraft._mc_capability`, and
  `MinecraftController._require_authorized`.
- **What:** `minecraft.control` is `CONFIRM`: one human confirmation starts a
  session. Gameplay capabilities (movement, look, combat, mining, build,
  items, inventory, interact, task, stop) are `ALLOW`, but only inside an
  authorised session. `minecraft.chat` and `minecraft.launch` are `CONFIRM`
  and not built. `minecraft.command` is `DENY`, permanently.
- **Fails closed:** an action name nobody wrote resolves to
  `minecraft.command`. `_require_authorized` runs inside the controller for
  every caller, the task runner included, so a skill cannot get round it.
  `minecraft/capabilities.py` can only subtract.
- **Tests:** `test_permissions.py`, `test_capabilities.py`,
  `test_minecraft_capabilities.py`, `test_minecraft_boundary.py`.

### 2. Session

- **Where:** `minecraft/session.py`; the banner in `actions/minecraft._mc_guard`.
- **What:** one session at a time, with no nesting and no extension. It lasts
  until stopped, or is timed (at most 300 s) if asked. It ends when the game
  exits, on F12 or `stop`, and when the game loses focus while a key is held.
- **What one confirmation covers** is said in four places that change
  together: `GRANT_SUMMARY`, the banner, the README's "What it deliberately
  cannot do", and `NOT_YET_POSSIBLE`. Tests check all four.
- **Tests:** `test_minecraft_controller.py`, `test_minecraft_actions.py`,
  and the consent-wording tests in `test_minecraft_attack_default.py` and
  `test_minecraft_interact_held_item.py`.

### 3. Focus guard

- **Where:** `MinecraftController._guard`, with `window.py` and `process.py`.
- **What:** checks that the session is live, the process alive, the window
  present and in the foreground. It runs before an action and on every 40 ms
  tick while anything is held, so a held key cannot outlive focus by more
  than one tick.
- **Tests:** `test_minecraft_safety.py`, `test_minecraft_controller.py`.

### 4. Ledger

- **Where:** `minecraft/ledger.py`.
- **What:** every held key and mouse button is recorded *before* it is
  pressed. `release_all()` is idempotent, works from any thread, takes no
  arguments and cannot be refused. Every stop path ends there, plus
  `atexit` and a `finally` on every action. Invariant: no input stays held
  after the controller has stopped.
- **Tests:** `test_minecraft_safety.py`, `test_minecraft_actions.py`.

### 5. Deadman

- **Where:** the controller's supervisor thread.
- **What:** releases anything held past its deadline (no hold is longer than
  `MAX_HOLD_SECONDS`, 12 s), even if the action thread is wedged and never
  ticks again.
- **Tests:** `test_minecraft_safety.py`, `test_minecraft_actions.py`.

F12 (`emergency.py`) is the fifth independent stop, alongside focus loss,
the deadman, session expiry and process death.

### 6. Hazard probe

- **Where:** injected into the controller as `hazard_probe`
  (`actions/minecraft._hazard_probe`), checked every 0.1 s inside a hold.
- **What:** lets go of a `mine`, `eat`, `interact` or `place` hold mid-hold
  when a hostile mob comes within 3 blocks or 2 health is lost since the hold
  began. Bridge only. Walking is exempt, because walking is how you get away.
- **Tests:** `test_minecraft_hazard_probe.py`.

### 7. Preconditions at the moment of pressing

The controller asks injected probes just before the button goes down. A
precondition can only refuse; it never redirects.

| Probe | Rule |
|---|---|
| `progress_probe` | `expect_at` / `expect_face`: the crosshair is on the exact block and face meant. |
| `entity_probe` | Every `attack`: a mob the game calls hostile is under the crosshair. Never a player, pet, villager or animal. No opt-out. |
| `held_item_probe` | `place` and `interact` refuse buckets, flint and steel, TNT, spawn eggs and similar. `use_item` pours lava or water or starts a fire only when the item is named (`expect_item`). |
| `gui_probe` | With any screen open, `attack`, `mine`, `place`, `interact`, `use_item`, `eat` and `drop` are refused. `inventory close` presses ESC only while a screen is reported open. `hotbar_select` is refused in a screen. Every one of these is judged on a reading taken after the last key that could open or close a screen, waiting up to a second for one; opening and closing wait for a reading that shows it, and say "opened", "closed" or "not confirmed". |

**Tests:** `test_minecraft_attack_default.py`,
`test_minecraft_attack_precondition.py`,
`test_minecraft_place_held_item.py`,
`test_minecraft_interact_held_item.py`, `test_minecraft_screen_guard.py`,
`test_minecraft_inventory_close.py`.

### 8. Click gate

- **Where:** `gui.click_refusal`, asked by the controller from a fresh bridge
  reading just before every `gui_click` and `gui_swap`.
- **What:** it allows only the inventory and the crafting table, in survival
  or adventure. The game must report the slot, with the pointer inside that
  slot's shrunken rectangle (so a click can never land between slots and
  drop the carried stack) and inside the window. No hostile mob may be near,
  no health lost since the screen opened, and the reading must be fresh. If
  any rule fails, nothing is pressed.
- **Tests:** `test_minecraft_gui.py`, `test_minecraft_gui_actions.py`.

### 9. Danger watch

- **Where:** `danger.DangerWatch`, run by `task_runner` before the first step
  and between every step.
- **What:** losing health, or a hostile mob within 5 blocks at about the
  player's level, stops the task and says which. A skill can stand it down
  only for what it handles itself: `navigate_to` keeps walking and reports
  what is close, and `flee` and `fight` keep their own health rules
  (`fight` will not start below 12 health and retreats below 8). The dusk
  note is overworld only: the Nether and the End have no night.
- **Tests:** `test_minecraft_danger.py`, `test_minecraft_tasks.py`,
  `test_minecraft_night.py`.

### Around all of it

- **Import boundary:** `test_minecraft_boundary.py` parses every module
  under `minecraft/`, recursively, plus the adapter. It allows no
  subprocess, network, file write, `exec`/`eval`, other action modules or OCR
  engines. New hooks are injected callables, never new imports.
- **The bridge mod is read-only:** it writes a snapshot file and never sends
  input, packets or commands. Schema changes are additive optional fields
  under `/4`, and the reader rejects unknown versions.
  `test_bridge_jar_is_current.py` keeps the shipped jar in step with
  the source.

## Tests

`tests/` mirrors the code. Each folder is a package, so unittest discovery
walks into it:

| Folder | What it tests |
|---|---|
| `tests/minecraft/` | The agent: `minecraft/`, `minecraft/skills/` and `actions/minecraft.py` |
| `tests/bridge/` | The bridge mod and its reader: the jar, its payload, reading it, and the mod installer (`tools/install_mod.py`) |
| `tests/core/` | Permissions, capabilities, audit, safe paths, safe exec, confirmation, enforcement, vulnerabilities, lint, repo hygiene (size budget, test isolation, import cycles, doc links, the project-structure tree) |
| `tests/voice/` | The voice pipeline, diagnostics, and `main.py`'s voice methods |
| `tests/dashboard/` | The phone dashboard |
| `tests/support/` | What several tests share; nothing here is a test |

`tests/support/` holds the simulated worlds (`sim_world.py`, `mob_world.py`,
`regression_worlds.py`, `gui_world.py`, `build_world.py`), fakes
(`fakes.py`), payload and fixture data (`bridge_payloads.py`,
`held_items.py`, `voice_paths.py`), the Java harness the bridge tests
compile against the mod's source (`java/`), and `paths.py`, which says
where the repository is. Tests import shared code from `tests.support`,
never from another test module, and find files through
`tests.support.paths`, never from their own location or the working
directory.

Run everything from the repository root:

```
python -m pytest
python -m unittest discover -s tests -t .
```

`-t .` matters: `tests/minecraft`, `tests/core` and `tests/dashboard` share
their names with app packages, and started from `tests/` alone unittest
would import them under those names. Their `__init__.py` refuses, saying
so. One file: `python -m pytest tests/minecraft/test_minecraft_fight.py`.

## Docs

The [readme](../readme.md) is what Jarvis is, installing and running it,
and safety in brief. Everything else is in `docs/`, indexed by
[docs/README.md](README.md); Minecraft has its own folder,
[docs/minecraft/](minecraft/README.md). `tests/core/test_repo_hygiene.py`
fails on a relative link that does not resolve.

## Known stale (fix at the next mod rebuild)

The mod's source cannot change without rebuilding
`mods/markliv-bridge-1.0.0.jar`, and the jar stays byte-identical until it
has been tried in the real game. So these wait for the next rebuild:

| Where | What is stale | Should say |
|---|---|---|
| `fabric-mod/src/main/java/com/markliv/bridge/MarkLivBridge.java`, line 637 (a comment) | `docs/minecraft-gui.md` | [`docs/minecraft/gui.md`](minecraft/gui.md) -- the doc moved |

## Where new code goes

**A new skill.** A class in the `minecraft/skills/` module for its kind
(`navigate`, `collect`, `collect_logs`, `collect_blocks`, `aim`, `eat`,
`craft`, `place_build`, `combat`), or a new module when none fits --
importing only from modules above it in the skills table. Register it in
`BUILTIN_SKILLS` and `__all__` in `minecraft/skills/__init__.py`; name it
in the tool text (`actions/_minecraft_text.py`: the TASKS paragraph and the
`task` parameter) and in [docs/minecraft/README.md](minecraft/README.md);
add it to the skills table here. If it changes what one session
confirmation covers, change `GRANT_SUMMARY`, the banner in
`actions/minecraft._mc_guard`, the "What it deliberately cannot do" list
in docs/minecraft/README.md and `NOT_YET_POSSIBLE` in the same commit.

**A new Minecraft action** (a key or button the controller presses).
Its limits in `minecraft/action_spec.py`; a controller method that goes
through `_require_authorized` and `_hold_inputs`; its capability in
`_CAPABILITY_BY_ACTION` (`actions/minecraft.py`), and in
`core/capabilities.py` if it is a new one. A new key goes on the
allowlist in `minecraft/input_backend.py` only with a reason, and
`test_minecraft_boundary.py` must still pass untouched. New checks are
injected callables, like `progress_probe` and `hazard_probe`, never new
imports.

**A new action for Jarvis** (not Minecraft). `actions/<name>.py` exposing
`TOOL` and a handler; discovery finds it. Helpers it alone uses go in
`actions/_<name>.py` -- a leading underscore keeps discovery away.

**A new mod field.** In the mod's source, an additive optional field
under schema `/4` (`markliv.minecraft.state/4`) -- never a new version,
since the reader rejects versions it does not know, and never anything
that writes to the game. Add its name to `FEATURES` in
`MarkLivBridge.java`, and, if Jarvis needs it, to `FEATURES` in
`minecraft/mod_bridge.py` with what goes without it: that list, not the
schema, is how an older `/4` jar is recognised and named. Read it in
`minecraft/mod_bridge.py` into
`WorldState` (`minecraft/state.py`) with its provenance. Rebuild
`mods/markliv-bridge-1.0.0.jar`; `test_bridge_jar_is_current.py` keeps
the jar and the source in step. Tests in `tests/bridge/`, with the Java
harness in `tests/support/java/`.

**A new test.** In the `tests/` folder for the code it tests. Anything a
second test needs goes in `tests/support/`; tests never import each other,
and find files through `tests.support.paths`.

**A new doc.** `docs/<topic>.md`, or `docs/minecraft/<topic>.md`, with a
line in the [docs index](README.md). Relative links are checked.

**Size.** No file outside `tests/` over 2,000 lines;
`tests/core/test_repo_hygiene.py` fails one that grows past it. Split
along the layering above rather than raise the limit.
