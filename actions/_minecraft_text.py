"""
actions/_minecraft_text.py -- the long text of the minecraft_control tool:
what the model reads about it (TOOL_DESCRIPTION) and about each parameter
(TOOL_PARAMETERS). Data only, no imports.

actions/minecraft.py builds TOOL from these and stays the one module the
action loader discovers; a file name starting with `_` is skipped by
discovery. Moved here verbatim from actions/minecraft.py.
"""

TOOL_DESCRIPTION = (
    "Plays Minecraft Java Edition. Use for any request about looking at "
    "or playing Minecraft.\n"
    "ONE CONFIRMATION: call start_session once, with no duration_s. The "
    "user approves a single banner and that covers ALL gameplay until "
    "they stop it — "
    "movement, looking, jumping, sprinting, sneaking, attacking, mining, "
    "placing, items, the hotbar, the inventory and interaction. Do NOT "
    "ask them to confirm individual actions, and do NOT call "
    "start_session again while one is open; if you are unsure, call "
    "status.\n"
    "READING (no session needed): status, observe, read_state, "
    "look_around, task_status. With the bridge mod, read_state and "
    "look_around read the game's own data: position, rotation, health, "
    "hunger, the inventory, the held item, the block under the crosshair "
    "(name, x, y, z, face) and the terrain around the player. Without "
    "it, read_state falls back to the F3 overlay (toggle_debug presses "
    "F3, and like every key press needs a session).\n"
    "find_ores (ore = coal, iron, copper, gold, redstone, lapis, diamond, "
    "emerald or quartz, or leave it out for any; radius default 16, up "
    "to 32) lists the ore the bridge's scan found, buried ones included, "
    "nearest first: where, how far, how deep below your feet, exposed or "
    "buried, and whether water or lava is near it. It presses nothing. It "
    "works only in a single-player world (on a server, finding ore inside "
    "rock is x-ray) and says so otherwise. Name only ore it lists; never "
    "promise ore it did not list.\n"
    "look_around is the one to use for 'what is around me', 'is there a "
    "tree nearby', 'any mobs'. It returns the nearest log, stone, water, "
    "ores and mobs with coordinates, from the bridge mod's terrain scan. "
    "If it says it cannot see the world, say that — do not describe a "
    "world from the crosshair or from memory.\n"
    "GAMEPLAY: move (direction, duration up to 2s), move_and_jump (walks "
    "and jumps together, up to 1s — the way onto a one-block ledge), "
    "look (dx/dy in PIXELS, up to 400 each — not degrees; to point at a "
    "particular block use run_task aim_at_block instead), jump, sneak "
    "and sprint (up to 2s), attack (up to 2s; it hits ONLY a hostile mob "
    "under the crosshair and refuses a player, a pet, a villager, an "
    "animal, a block or nothing -- break blocks with mine), mine (one "
    "continuous hold "
    "of up to 10s that lets go when the bridge sees the block change), "
    "place (refused, before anything is pressed, unless the hand holds a "
    "block: never a bucket, flint and steel, TNT, a spawn egg, a pearl, a "
    "potion, a bow or a tool -- and never when the hand cannot be seen), "
    "interact (up to 1s; the same held-item refusal as place, except an "
    "empty hand is fine), use_item (up to 2s; pouring a lava, water or "
    "powder-snow bucket or using flint and steel or a fire charge needs "
    "expect_item naming it, only when the user named it), eat (up to "
    "3s), drop (one "
    "item), hotbar_select (slot 1-9), inventory (state=open|close; "
    "close presses ESC only while the bridge reports a screen open -- "
    "with none open ESC brings up the pause menu, so it is refused), "
    "stop. Longer durations are shortened to the limit, not refused. "
    "While any screen is open (the bridge says which), attack, mine, "
    "place, interact, use_item, eat and drop are refused -- their "
    "clicks would land in the screen; close it first with inventory "
    "close.\n"
    "TASKS: run_task does a bounded multi-step job, observing and "
    "verifying between steps: walk_forward, survey, find_block, "
    "break_block, place_block, collect_logs, fell_tree, collect_blocks, "
    "craft_item, place_block_at, build_line, build_blueprint, "
    "navigate_to, flee, fight, eat_food, aim_at_block, "
    "mine_block, dig_to, mine_ore. The mining tasks (break_block, collect_logs, fell_tree, "
    "collect_blocks, mine_block) take up the best tool in the HOTBAR first and put the "
    "slot back afterwards. When the tool that harvests the block -- or "
    "one much faster -- is only in the main inventory, they first move "
    "it into the hotbar through the inventory screen (one number-key "
    "swap, closing the screen if a hostile comes within 8 blocks or you "
    "are hurt) and say which slot it went to. They refuse, before "
    "swinging, a block nothing they can hold will harvest (stone with "
    "no pickaxe anywhere, iron with a wooden pickaxe) and say why. A task RUNS IN THE "
    "BACKGROUND: run_task answers "
    "'started' at once, and when the task ends a message beginning "
    "[Minecraft task] reports what actually happened — relay that, and "
    "never say a task worked before it arrives. A task stops itself "
    "after 45 steps, after two minutes, or when it detects it is making "
    "no progress. It also stops — or does not start — if you are taking "
    "damage or a hostile mob is within 5 blocks, and says which: tell "
    "the user straight away, since they may need to fight or run. "
    "navigate_to is the exception — walking is how to get away — so it "
    "keeps going and ends with a note of any mob close by; for the same "
    "reason move, sprint and sneak are never stopped by a mob or by "
    "damage. Only one runs at a time, and while it runs the other "
    "gameplay actions are refused; task_status says how it is going.\n"
    "STOPPING: if the user says stop, halt or cancel while a task or "
    "action is running, it is stopped at once and every key released "
    "(the control session stays open). cancel_task does the same. stop "
    "(the action) also ENDS the control session; F12 is the hard stop "
    "and ends it too.\n"
    "aim_at_block and mine_block take x, y and z — a block look_around "
    "or read_state actually reported. They turn until the game itself "
    "confirms the crosshair is on that exact block; mine_block then "
    "breaks it and checks that the block at that coordinate is gone. "
    "Neither mines anything if the aim cannot be confirmed, and neither "
    "walks: if the block is out of reach, navigate_to first.\n"
    "flee gets away from hostile mobs: it runs -- sprinting on clear "
    "straight ground -- to the place it can reach that is furthest "
    "from every hostile it sees, keeping clear of them on the way, "
    "until the nearest is over 12 blocks off (seconds: how long to "
    "keep trying, 30 by default). A mob close by or damage never "
    "stops it -- that is when to use it. Use it when the user says "
    "run, get away, or is being chased; tell them where the mob "
    "ended up.\n"
    "fight is OPT-IN: start it only when the user asks to fight or "
    "attack a mob, never on your own. It fights the nearest hostile mob "
    "within 16 blocks (target: a kind, e.g. zombie): walks into reach, "
    "aims at its body, and hits in short taps -- every swing refused "
    "unless the game reports a hostile under the crosshair, so never a "
    "player or an animal. It will not walk up to a creeper, a warden, "
    "an enderman, a piglin or zombified piglin, a ghast, a blaze, a "
    "guardian, a ravager, a boss or the like (it says why), does not "
    "start below 12 health, takes up the best sword (else axe) in the "
    "hotbar and puts the slot back after, retreats with flee below 8 "
    "health, and stops after 20 seconds. The game does not report a "
    "mob's health: "
    "'gone' means most likely killed, so say that, not 'killed'.\n"
    "navigate_to walks somewhere, routing round obstacles: give it "
    "either x and z, or target='log'|'stone'|'water'. It refuses a "
    "destination outside the scanned area instead of setting off "
    "hopefully — if it says it cannot see that far, relay that rather "
    "than retrying with a bigger number. If it says it stopped N blocks "
    "short because the task ran out of steps, call it again with the "
    "same destination: it carries on from where it is.\n"
    "collect_logs walks to the nearest tree it can reach, breaks logs "
    "until it has the count, and walks over the drops to pick them up. "
    "It finishes one tree before starting another. fell_tree takes "
    "every log it can reach from ONE tree — the one in front, else the "
    "nearest — picks them up and stops: use it for 'chop down the "
    "tree', 'mine the tree' or 'the rest of the tree', not collect_logs "
    "with a guessed count. Both break leaves in the way first — a few "
    "at most, never counted as logs. Their reports name the tree each "
    "log came from and any logs left too high to reach: answer 'which "
    "tree' or 'why that tree' from that report, and if it does not say, "
    "say you do not know rather than guess. "
    "collect_blocks gets `count` of target = stone (cobblestone), dirt, "
    "sand, red_sand, gravel, deepslate, coal, iron (raw iron) or copper "
    "(raw copper): it walks to the nearest exposed one it can reach, "
    "breaks it with the right hotbar tool and picks up the drop, and "
    "counts success ONLY from the drop arriving in the inventory. It "
    "never digs (a buried block is 'not reachable without digging'), "
    "never breaks the block under the player, and leaves any block "
    "touching water or lava. break_block only breaks "
    "whatever the crosshair is on right now — it does not aim or walk — "
    "so for 'break a log' use collect_logs.\n"
    "craft_item makes `count` of `item` by clicking in the crafting grid "
    "-- the inventory's 2x2, or a crafting table's 3x3 within reach for "
    "pickaxes, axes, swords, shovels, hoes, furnaces and chests. It "
    "knows planks (any log), stick, crafting_table, wooden_ and stone_ "
    "tools, furnace, torch and chest. It checks the ingredients first "
    "and says what is missing; it never clicks anywhere the game does "
    "not report the pointer over the slot it means, closes the screen "
    "and stops if a hostile comes within 8 blocks or you are hurt, and "
    "proves the result from the inventory (output up, ingredients "
    "down). No crafting table near for a 3x3 recipe: if one is in the "
    "inventory it puts it down beside you first (the same way as "
    "place_block_at), otherwise it says so -- craft_item crafting_table "
    "makes one. "
    "place_block_at puts ONE block of `item` -- dirt, cobblestone, "
    "stone, planks or another plain building block -- into the cell x, "
    "y, z. It refuses a cell that holds a block or that it cannot see "
    "is empty, and a cell with nothing solid beside it to place "
    "against. It never places against a chest, door or crafting table. "
    "It stands within reach and out of the cell, presses only when the "
    "game reports the crosshair on the right face, and proves the "
    "result twice: the block at that cell under the crosshair, and the "
    "stack one smaller. "
    "build_line places up to 16 blocks in a straight line from x, y, z "
    "(direction north | south | east | west | up, count, item), each "
    "one the same way. Its report names every cell placed and every "
    "cell that failed, with why, and the inventory count -- relay that, "
    "not the count asked for. From the ground a column (up) is two "
    "blocks high: it cannot jump and place yet. If it ran out of steps, "
    "call it again from the next cell. "
    "build_blueprint builds a named plan bottom-up: plan = platform "
    "(size 2-5 across), wall (size 2-8 long, 2 high) or shelter (hollow "
    "3x3x3, walls two high with a doorway, a roof, and a three-block "
    "step along one side to stand on for the roof; 26 blocks). x, y, z are the centre at "
    "the level the player stands in (leave them out for three blocks "
    "in front); direction is the side the doorway faces (default: "
    "towards the player). Limits: at most 64 blocks, every cell within "
    "6 blocks of where it starts, only empty cells or grass, flat "
    "ground, plain blocks, and enough of them for the whole plan -- "
    "otherwise it refuses before pressing anything. BEFORE calling it, "
    "tell the user the plan in one sentence: what, of which block, "
    "where, how many blocks. A task places about fifteen blocks; if "
    "the report says to ask again, call build_blueprint with the same "
    "plan and no coordinates to carry on (a shelter takes three or "
    "four tasks). Relay its report: which "
    "cells were placed, which failed, and the inventory count. "
    "eat_food eats until not hungry (count: how many items at most): it "
    "picks food that will not make you ill and does not waste golden "
    "apples, looks up first if a chest or door is under the crosshair, "
    "and puts the held slot back afterwards. With no food on the hotbar "
    "it moves food from the main inventory into the hotbar first, the "
    "same way; if it says it could not (an old mod, creative mode, a "
    "mob close by), relay why and ask the user to move it. "
    "dig_to digs a staircase until the feet are at x, y, z -- ONLY when "
    "the user asks to dig, and only in a single-player world. At most one "
    "block down (or up) per block across, never straight down, never the "
    "block underfoot; only natural stone, earth and ore; it stops before "
    "breaking anything beside water or lava, within 3 of lava, or under "
    "sand or gravel; at most 40 blocks, 16 below and 32 away from where "
    "it began; and it stops at once if the user is hurt, fluid comes "
    "near, a block falls in, or the player falls. If it opens into a "
    "cave it stops there and says so. Before calling it, tell the user "
    "where it will dig and how deep. A task digs a few stairs; if the "
    "report says to ask again, call dig_to with the same x, y, z. "
    "mine_ore (ore, count 1-16, radius) goes for the nearest ore of that "
    "kind the scan lists that it can reach safely -- ONLY when the user "
    "asks for ore, and only in a single-player world. It passes over ore "
    "with water or lava beside it, digs a staircase to the cell beside "
    "the ore under every dig_to rule (an exposed ore within reach needs "
    "none), then breaks the ore of that vein it can reach and see, steps "
    "into the hole for the rest and to pick the drops up, and reports "
    "what the inventory gained. If a rule refuses the way (lava near, "
    "water, sand or gravel) it stops and names the nearest other ore it "
    "could go for. BEFORE calling it, call find_ores and tell the user "
    "the plan: which ore, how far, how deep, about how many blocks it "
    "will dig. Never promise ore the scan did not list, and never say "
    "how much it got until the report gives the inventory count. If the "
    "report says to ask again, call mine_ore with the same ore.\n"
    "AT DUSK OR NIGHT: when look_around says it is getting dark or "
    "night, or a task result notes it with a hostile mob about, tell "
    "the user before anything else and offer to build a shelter "
    "(build_blueprint shelter) or to flee -- do not keep working "
    "silently, and do not start a long task in the dark without "
    "saying so.\n"
    "REPORTING RESULTS HONESTLY — this matters most:\n"
    "  * Holding attack is not breaking a block. Never say a block broke, "
    "a tree was chopped or wood was collected unless "
    "verification.status == 'success' or the task report says so.\n"
    "  * 'unverifiable' means I could not SEE whether it worked. Say "
    "that plainly; do not treat it as success or as failure.\n"
    "  * Whether I can confirm an item was PICKED UP depends on the "
    "bridge mod. With it, the inventory is readable and 'collected' is a "
    "real claim. Without it I only see a block disappear — then say "
    "'broke', not 'collected'. The task result says which one it used; "
    "do not upgrade 'broke' to 'collected'.\n"
    "  * An unknown block is not air, and a place I have not scanned is "
    "not empty ground. If something says UNKNOWN, report UNKNOWN.\n"
    "  * ok == true only means the input reached the game.\n"
    "Input only goes to Minecraft while it is the window in front: if "
    "the user alt-tabs away, anything held is released within one tick "
    "and a running task stops."
)

TOOL_PARAMETERS = {
    "type": "OBJECT",
    "properties": {
        "action": {
            "type": "STRING",
            "description": (
                "status | observe | read_state | look_around | "
                "find_ores | "
                "task_status | toggle_debug | "
                "start_session | end_session | move | move_and_jump | "
                "look | jump | "
                "sneak | sprint | attack | mine | place | interact | "
                "use_item | eat | drop | hotbar_select | inventory | "
                "run_task | cancel_task | stop"),
        },
        "direction": {
            "type": "STRING",
            "description": ("For move/move_and_jump/sneak/sprint: "
                            "forward | back | left | right. For "
                            "run_task build_line: north | south | east "
                            "| west | up. For build_blueprint: the side "
                            "the doorway (or the wall's face) points to "
                            "-- north | south | east | west."),
        },
        "duration": {
            "type": "NUMBER",
            "description": ("Seconds to hold. Limits: move, sneak, "
                            "sprint, attack, use_item, eat 2.0; mine "
                            "10.0; move_and_jump and interact 1.0. "
                            "Longer requests are shortened to the "
                            "limit, not refused. place, drop, jump, "
                            "hotbar_select and inventory are taps and "
                            "take no duration."),
        },
        "dx": {
            "type": "INTEGER",
            "description": ("For look: horizontal mouse movement in "
                            "pixels, -400 to 400. Positive turns right."),
        },
        "dy": {
            "type": "INTEGER",
            "description": ("For look: vertical mouse movement in pixels, "
                            "-400 to 400. Positive looks down."),
        },
        "slot": {
            "type": "INTEGER",
            "description": ("For hotbar_select: which slot, 1 to 9. Out "
                            "of range is refused, not adjusted."),
        },
        "duration_s": {
            "type": "NUMBER",
            "description": ("For start_session. LEAVE THIS OUT unless the "
                            "user asks for a time limit — the session "
                            "then runs until they stop it, which is what "
                            "they usually want. A number gives a timed "
                            "session, up to 300 seconds."),
        },
        "state": {
            "type": "STRING",
            "description": "For inventory: open | close.",
        },
        "task": {
            "type": "STRING",
            "description": ("For run_task: walk_forward | survey | "
                            "find_block | break_block | place_block | "
                            "collect_logs | fell_tree | collect_blocks | "
                            "craft_item | place_block_at | build_line | "
                            "build_blueprint | navigate_to | flee | "
                            "fight | eat_food | aim_at_block | "
                            "mine_block | dig_to | mine_ore."),
        },
        "x": {
            "type": "INTEGER",
            "description": ("For run_task navigate_to, aim_at_block, "
                            "mine_block, place_block_at, build_line "
                            "(the first cell) and dig_to (the cell to "
                            "stand in at the end): the block's "
                            "x. Use a coordinate look_around or "
                            "read_state actually reported, or one "
                            "worked out from it; do not invent one."),
        },
        "y": {
            "type": "INTEGER",
            "description": ("For run_task aim_at_block, mine_block, "
                            "place_block_at, build_line and dig_to: the "
                            "block's y."),
        },
        "z": {
            "type": "INTEGER",
            "description": ("For run_task navigate_to, aim_at_block, "
                            "mine_block, place_block_at, build_line and "
                            "dig_to: the block's z. Needs x as well."),
        },
        "target": {
            "type": "STRING",
            "description": ("For run_task navigate_to: walk to the "
                            "nearest one of these instead of a "
                            "coordinate — log, stone, dirt, grass, sand, "
                            "water, crafting_table, furnace, chest, or "
                            "an exact block name. For collect_blocks: "
                            "what to collect — stone, dirt, sand, "
                            "red_sand, gravel, deepslate, coal, iron or "
                            "copper."),
        },
        "item": {
            "type": "STRING",
            "description": ("For craft_item: what to make — e.g. "
                            "oak_planks, stick, crafting_table, "
                            "wooden_pickaxe, stone_axe, furnace, torch, "
                            "chest. For place_block_at, build_line and "
                            "build_blueprint: the block to place — e.g. "
                            "cobblestone, dirt, oak_planks."),
        },
        "expect_item": {
            "type": "STRING",
            "description": ("For use_item: the item the user named, "
                            "e.g. lava_bucket. Needed to pour a bucket "
                            "or start a fire; it checks the hand, it "
                            "does not select anything."),
        },
        "plan": {
            "type": "STRING",
            "description": ("For run_task build_blueprint: platform | "
                            "wall | shelter."),
        },
        "size": {
            "type": "INTEGER",
            "description": ("For run_task build_blueprint: a "
                            "platform's width (2-5) or a wall's length "
                            "(2-8). The shelter has one size."),
        },
        "count": {
            "type": "INTEGER",
            "description": ("For build_line: how many blocks (1 to "
                            "16). For mine_ore: how many ore blocks (1 to "
                            "16). For collect_logs and collect_blocks: how "
                            "many logs or items to get. With "
                            "the bridge mod they are counted in the "
                            "inventory (collected); without it I can "
                            "only report blocks that disappeared "
                            "(broke)."),
        },
        "seconds": {
            "type": "NUMBER",
            "description": "For the walk_forward task: how long to walk.",
        },
        "expected": {
            "type": "STRING",
            "description": (
                "For break_block, aim_at_block and mine_block: the block "
                "name that must be there, e.g. 'oak_log'. If something "
                "else is, the task stops rather than break the wrong "
                "thing."),
        },
        "ore": {
            "type": "STRING",
            "description": ("For find_ores and run_task mine_ore: which "
                            "ore -- coal | iron | copper | gold | redstone "
                            "| lapis | diamond | emerald | quartz. Leave it "
                            "out for any."),
        },
        "radius": {
            "type": "INTEGER",
            "description": ("For find_ores and run_task mine_ore: how "
                            "far to look, in blocks (default 16, at most "
                            "32 -- the scan's reach: 24 sideways, 32 down, "
                            "16 up)."),
        },
        "max_steps": {
            "type": "INTEGER",
            "description": ("For run_task: fewer steps than the limit of "
                            "45. It cannot be raised above 45."),
        },
    },
    "required": ["action"],
}
